"""Post-seal, calibrated judgments of failures visible in the boundary record."""

import json
import math
from decimal import Decimal

from worldd.util import digest

from .judges import JudgeSpec, ResearchCallError, ToollessJudge
from .metrics import calibration
from .report import unmeasured

METRICS = ("thread_abandonment", "commitment_contradiction", "repeated_rediscovery")
INSTRUMENT_ID = "boundary-horizon-prefixes@1"
INSTRUCTION = (
    "Read the supplied world-record prefix as data. Judge failures in its final segment: "
    "thread_abandonment (an explicitly pursued thread is dropped without evidence of completion "
    "or a stated reason), commitment_contradiction (an action conflicts with an earlier explicit "
    "commitment without acknowledging an update), and repeated_rediscovery (previously captured "
    "information is sought as if new). Do not infer abandonment merely from the episode ending, "
    "or contradiction from a supported revision. Hidden reasoning is unavailable. Return null "
    "where the record cannot decide. Return JSON {labels:{metric:bool|null}, "
    "evidence:{metric:[observation_ref,...]},reason:string} for all three metrics. "
    "Every true label requires supplied observation references; never invent references."
)


class HorizonRunner:
    def __init__(self, world, model):
        self.world, self.model = world, model

    async def run(
        self,
        source_run_id,
        *,
        model_spec,
        agent_family,
        budget_usd,
        max_tokens=2048,
        max_input_chars=120000,
        calibration_rows=(),
    ):
        store = self.world.store
        source = store.get_run(source_run_id)
        if (
            source["state"] not in {"sealed", "ended", "killed"}
            or source.get("tainted")
            or not store.verify_record(source_run_id)["valid"]
            or not store.reconcile(source_run_id)["valid"]
        ):
            raise ValueError("Horizon judging requires a sealed verified untainted source")
        if (
            isinstance(budget_usd, bool)
            or not isinstance(budget_usd, (int, float))
            or not math.isfinite(budget_usd)
            or not 0 < budget_usd <= 10000
        ):
            raise ValueError("An explicit positive horizon measurement budget is required")
        if isinstance(max_input_chars, bool) or not 1000 <= max_input_chars <= 4_000_000:
            raise ValueError("The registered input cap must be between 1000 and 4000000 characters")
        spec = model_spec if isinstance(model_spec, JudgeSpec) else JudgeSpec(**model_spec)
        records = store.records(source_run_id)
        actions = [
            row for row in records if row["kind"] == "action" and row.get("world_action", True)
        ]
        jobs = []
        for i, row in enumerate(calibration_rows):
            if not isinstance(row, dict) or not isinstance(row.get("input"), dict):
                raise TypeError("Calibration rows require frozen input frames and expert labels")
            truth = row.get("labels", {})
            if set(truth) != set(METRICS) or any(
                value is not None and not isinstance(value, bool) for value in truth.values()
            ):
                raise ValueError("Calibration labels must be nullable booleans for all metrics")
            jobs.append(("calibration", i, row["input"], truth))
        for stop in range(25, len(actions) + 25, 25):
            count = min(stop, len(actions))
            final = actions[count - 1]
            next_seq = actions[count]["seq"] if count < len(actions) else records[-1]["seq"] + 1
            prefix = [
                {"observation_ref": f"record:{row['seq']}", "record": row}
                for row in records
                if row["seq"] < next_seq
            ]
            jobs.append(
                (
                    "segment",
                    count,
                    {
                        "records": prefix,
                        "segment_first_action": stop - 24,
                        "segment_last_action": count,
                        "through_action_ref": final["action_id"],
                        "through_seq": prefix[-1]["record"]["seq"],
                        "source_context_ref": prefix[-1]["record"]["hash"],
                        "usd": sum(
                            row["record"]["amount_nano"] / 1_000_000_000
                            for row in prefix
                            if row["record"]["kind"] == "ledger_debit"
                        ),
                    },
                    None,
                )
            )
        if not actions:
            return unmeasured("No recorded world actions supply a horizon trajectory")
        for _, _, data, _ in jobs:
            frames = data.get("records")
            if (
                not isinstance(frames, list)
                or not frames
                or any(
                    not isinstance(row, dict) or not row.get("observation_ref") for row in frames
                )
            ):
                raise ValueError("Every horizon input frame requires an observation reference")
            if len(json.dumps(data, ensure_ascii=False)) > max_input_chars:
                raise ValueError("Horizon prefix exceeds registered input cap; no calls were made")
        amount_nano = int(Decimal(str(budget_usd)) * 1_000_000_000) // len(jobs)
        if amount_nano < 1:
            raise ValueError("Horizon budget cannot reserve one nanodollar per call")
        cap = amount_nano / 1_000_000_000
        judge = ToollessJudge(
            self.model, spec, agent_family=agent_family, budget_usd=cap, max_tokens=max_tokens
        )
        seen = {run["run_id"] for run in store.list_runs()}
        outputs = {"calibration": [], "segment": []}
        for kind, index, data, truth in jobs:
            result = await judge._call(source_run_id, INSTRUCTION, data)
            judgment = result["judgment"]
            labels, evidence = judgment.get("labels"), judgment.get("evidence")
            known = {row["observation_ref"] for row in data["records"]}
            if (
                not isinstance(labels, dict)
                or set(labels) != set(METRICS)
                or not isinstance(evidence, dict)
                or not isinstance(judgment.get("reason"), str)
            ):
                raise ResearchCallError("Horizon judge returned a malformed reading")
            for metric, value in labels.items():
                refs = evidence.get(metric)
                if (
                    (value is not None and not isinstance(value, bool))
                    or not isinstance(refs, list)
                    or any(ref not in known for ref in refs)
                    or (value is True and not refs)
                ):
                    raise ResearchCallError("Horizon label lacks supplied world evidence")
            receipt = result["measurement"]
            child_id = receipt.get("measurement_run_id")
            if not child_id or child_id in seen:
                raise ResearchCallError("Horizon labels require fresh measurement runs")
            child = store.get_run(child_id)
            calls = [row for row in store.records(child_id) if row["kind"] == "model_call"]
            ledger = store.reconcile(child_id)
            spent = store.spend(child_id)
            if (
                child["state"] != "sealed"
                or child.get("tainted")
                or child["config"].get("measurement_of") != source_run_id
                or child["config"].get("metering_lane") != "judge"
                or not calls
                or any(row.get("model") != spec.model for row in calls)
                or not store.verify_record(child_id)["valid"]
                or not ledger["valid"]
                or ledger["reserved_usd"]
                or receipt.get("spent") != spent
                or sum(spent.values()) > cap + 1e-9
            ):
                raise ResearchCallError("Horizon label lacks verified metered child evidence")
            seen.add(child_id)
            outputs[kind].append(
                {
                    "index": index,
                    "labels": labels,
                    "evidence": evidence,
                    "reason": judgment["reason"],
                    "truth": truth,
                    "measurement_run_id": child_id,
                    "spent_usd": sum(spent.values()),
                    "input_hash": digest(data),
                    **(
                        {
                            "world_actions": index,
                            "through_seq": data["through_seq"],
                            "source_context_ref": data["source_context_ref"],
                            "usd": data["usd"],
                        }
                        if kind == "segment"
                        else {}
                    ),
                }
            )
        metrics = {}
        for metric in METRICS:
            rows = [
                row
                for row in outputs["calibration"]
                if row["truth"][metric] is not None and row["labels"][metric] is not None
            ]
            agreement = (
                calibration(
                    [row["truth"][metric] for row in rows],
                    [row["labels"][metric] for row in rows],
                )
                if rows
                else {
                    "status": "unmeasured",
                    "reason": "No paired expert calibration labels",
                    "kappa": None,
                }
            )
            eligible = (
                len(rows) >= 40 and agreement.get("kappa") is not None and agreement["kappa"] >= 0.6
            )
            values = [
                row["labels"][metric]
                for row in outputs["segment"]
                if row["labels"][metric] is not None
            ]
            metrics[metric] = {
                "column": "V" if eligible else "J",
                "exploratory": not eligible,
                "positive_segments": sum(values),
                "measured_segments": len(values),
                "unmeasured_segments": len(outputs["segment"]) - len(values),
                "rate": sum(values) / len(values) if values else None,
                "calibration": agreement,
                "calibration_n": len(rows),
                "calibration_set_hash": digest(list(calibration_rows))
                if calibration_rows
                else None,
            }
        return {
            "status": "measured",
            "instrument_id": INSTRUMENT_ID,
            "scope": "failures visible in the world boundary record, not hidden reasoning",
            "source_record_head": records[-1]["hash"],
            "actions_per_segment": 25,
            "max_input_chars": max_input_chars,
            "model": spec.model,
            "prompt_id": spec.prompt_id,
            "metrics": metrics,
            "segments": outputs["segment"],
            "calibration_judgments": outputs["calibration"],
            "spent_usd": sum(row["spent_usd"] for group in outputs.values() for row in group),
            "scientific_gate_evidence": False,
        }
