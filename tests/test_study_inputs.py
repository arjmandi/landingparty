import hashlib
import json

import pytest

from worldd.storage import Store
from worldd.study_inputs import freeze_protocol, verify_protocol


@pytest.fixture
def inputs(tmp_path):
    store = Store(tmp_path)
    private = tmp_path / "control" / "study-inputs"
    private.mkdir(parents=True)

    def write(name, value):
        path = private / (name + ".json")
        raw = json.dumps(value, sort_keys=True).encode()
        path.write_bytes(raw)
        return {
            "path": str(path.relative_to(tmp_path)),
            "hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }

    questions = [
        f"Question {number}: what independently verifiable event happened?" for number in range(25)
    ]
    text = "Frozen post-cutoff event\n" + "\n".join(questions)
    question_ref = write("questions", questions)
    trap_ref = write(
        "traps",
        [{"id": f"trap-{n}", "criterion": f"Check unsupported claim type {n}"} for n in range(8)],
    )
    goal_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    protocol = {"goal_text_hash": goal_hash, "questions": question_ref, "traps": trap_ref}
    descriptor = write("protocol", protocol)
    prereg = {
        "frozen_goal_hash": goal_hash,
        "frozen_questions_hash": question_ref["hash"],
        "frozen_traps_hash": trap_ref["hash"],
    }
    yield store, descriptor, prereg, [{"config": {"goal": text}}], write, protocol
    store.close()


def test_actual_private_materials_copy_to_immutable_study_and_verify_before_run(inputs):
    store, descriptor, prereg, cells, _, _ = inputs
    frozen = freeze_protocol(store, "study_real", descriptor, prereg, cells)
    study = {"study_id": "study_real", "goal_protocol": frozen}
    verify_protocol(store, study)
    assert (store.data_dir / frozen["questions"]["path"]).is_file()
    (store.data_dir / frozen["traps"]["path"]).write_text("[]")
    with pytest.raises(ValueError, match="changed or disappeared"):
        verify_protocol(store, study)


def test_hash_count_and_actual_agent_question_delivery_are_required(inputs):
    store, descriptor, prereg, cells, write, protocol = inputs
    with pytest.raises(ValueError, match="pin the actual frozen questions"):
        freeze_protocol(
            store,
            "study_wrong_hash",
            descriptor,
            {**prereg, "frozen_questions_hash": "sha256:invented"},
            cells,
        )
    protocol["questions"] = write("questions-short", [f"Question {n}" for n in range(24)])
    short = write("protocol-short", protocol)
    with pytest.raises(ValueError, match="exactly 25"):
        freeze_protocol(store, "study_short", short, prereg, cells)
    shortened = [{"config": {"goal": "An unrelated replacement task"}}]
    with pytest.raises(ValueError, match="frozen goal text"):
        freeze_protocol(store, "study_task_swapped", descriptor, prereg, shortened)


def test_agent_workspace_cannot_supply_private_hidden_materials(inputs):
    store, descriptor, prereg, cells, _, _ = inputs
    run = store.create_run({"budget": {"usd": 1}})["run_id"]
    path = store.run_dir(run) / "work" / "protocol.json"
    path.parent.mkdir()
    path.write_bytes((store.data_dir / descriptor["path"]).read_bytes())
    with pytest.raises(ValueError, match="control/study-inputs"):
        freeze_protocol(
            store,
            "study_agent_authored",
            {"path": str(path.relative_to(store.data_dir)), "hash": descriptor["hash"]},
            prereg,
            cells,
        )
