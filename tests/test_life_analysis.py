import hashlib
import json
from datetime import timedelta

import pytest

from analysis.pipeline import PostRunPipeline
from tests.test_rubric import policy
from worldd.footprint import FootprintService
from worldd.lives import LifeService
from worldd.models import RunConfig
from worldd.registry import RegistryService
from worldd.runtime import World
from worldd.storage import Store


class Gateway:
    async def aclose(self):
        pass


@pytest.fixture
async def life_source(tmp_path):
    world = World(Store(tmp_path), Gateway())
    world.services["registry"] = RegistryService(world)
    lives = world.services["lives"] = LifeService(world)
    footprint = world.services["footprint"] = FootprintService(world)
    life_id = lives.create({"stage": 1, "binding_id": "fixture-binding"})["life_id"]
    run_id = world.create_run(RunConfig(stage=1, life_id=life_id))["run_id"]
    item = footprint.record_item(run_id, "a_write", kind="citation", platform="fixture")
    await world.end_run(run_id)
    for _ in range(5):
        footprint.add_reading(item["item_id"], 1, author_class="independent_human")
    root = world.store.run_dir(run_id)

    def artifact(name, payload):
        path = root / name
        path.write_text(json.dumps(payload))
        return {"path": name, "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}

    binding = {
        "binding_id": "fixture-binding",
        "counterparty_classes": ["independent_human"],
        "evidence_mappings": [
            {
                "parameter": "purpose",
                "column": "G",
                "evidence_class": "uptake",
                "item_kind": "citation",
            }
        ],
    }
    pipeline = world.services["pipeline"] = PostRunPipeline(world)
    options = {
        "source_run_id": run_id,
        "policy_artifact": artifact("rubric-policy.json", policy()),
        "binding_artifact": artifact("rubric-binding.json", binding),
    }
    try:
        yield world, lives, pipeline, life_id, run_id, options, artifact
    finally:
        await world.aclose()


async def test_actual_life_checkpoint_persists_private_immutable_report(life_source):
    world, lives, pipeline, life_id, run_id, options, _ = life_source
    assert not pipeline.has_life_config(life_id)
    configured = pipeline.configure_life(life_id, **options)
    assert configured["configured"] and pipeline.has_life_config(life_id)
    before = world.store.records(run_id)
    with pytest.raises(ValueError, match="ended life"):
        await pipeline.life(life_id)
    await lives.end(life_id)
    result = await pipeline.life(life_id)
    assert result["report"]["rubric"]["purpose"]["G"]["value"] == 1
    assert result["report"]["rubric"]["connection"]["G"]["status"] == "unmeasured"
    path = world.store.data_dir / result["artifact_ref"]
    assert result["artifact_hash"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert lives.get(life_id)["life_rubric_ref"]["artifact_hash"] == result["artifact_hash"]
    publication = world.life_report(life_id)["publication"]
    assert (
        publication["figure_svgs"] and "Revealed life parameters" in publication["figure_svgs"][-1]
    )
    export_root = (
        world.store.data_dir
        / "lives"
        / life_id
        / "publication"
        / result["artifact_hash"].split(":", 1)[1]
    )
    assert (export_root / "rubric-matrix.png").is_file()
    assert (export_root / "curve-points.csv").is_file()
    assert await pipeline.life(life_id) == result
    footprint = world.services["footprint"]
    item = footprint.query(life_id=life_id)[0]
    footprint.add_reading(item["item_id"], 100, author_class="independent_human")
    assert await pipeline.life(life_id) == result  # Later evidence cannot leak into an end score.
    assert world.store.records(run_id) == before


async def test_life_analysis_refuses_changed_config_and_premature_calendar_reading(life_source):
    _world, _lives, pipeline, life_id, _run_id, options, artifact = life_source
    pipeline.configure_life(life_id, **options)
    changed = policy()
    changed["policy_id"] = "different"
    options["policy_artifact"] = artifact("different.json", changed)
    with pytest.raises(ValueError, match="immutable"):
        pipeline.configure_life(life_id, **options)
    with pytest.raises(ValueError, match="not due"):
        await pipeline.life(life_id, checkpoint="d7")
    with pytest.raises(ValueError, match="actual afterlife"):
        await pipeline.life(life_id, checkpoint="after30")


async def test_scoring_rechecks_actual_frozen_policy_bytes_and_research_registration(life_source):
    world, lives, pipeline, life_id, _run_id, options, _artifact = life_source
    pipeline.configure_life(life_id, **options)
    path = world.store.data_dir / "lives" / life_id / "rubric/config.json"
    config = json.loads(path.read_bytes())
    research = {**lives.get(life_id), "research_mode": "research"}
    with pytest.raises(ValueError, match="registered series"):
        pipeline.life_pipeline._validate_configuration(research, config)
    config["policy"]["policy_id"] = "post-hoc-changed-shape-policy"
    path.write_text(json.dumps(config))
    await lives.end(life_id)
    with pytest.raises(ValueError, match="frozen artifact"):
        await pipeline.life(life_id)


async def test_automatic_afterlife_checkpoint_uses_actual_reading_without_reopening_source(
    life_source,
):
    world, lives, pipeline, life_id, run_id, options, _ = life_source
    pipeline.configure_life(life_id, **options)
    await lives.end(life_id)
    before = world.store.records(run_id)
    origin = lives._now()
    lives.clock = lambda: origin + timedelta(days=31)
    await lives.tick(life_id)
    life = lives.get(life_id)
    assert life["afterlife"]["30"]["state"] == "complete"
    assert life["life_rubric_ref"]["checkpoint"] == "after30"
    assert world.store.records(run_id) == before
