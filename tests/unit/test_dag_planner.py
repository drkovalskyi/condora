"""Unit tests for DAG Planner offset consumption, adaptive capping, and pileup resolution."""

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from wms2.adapters.mock import MockCondorAdapter, MockDBSAdapter, MockRucioAdapter
from wms2.config import Settings
from wms2.core.dag_planner import DAGPlanner


def _make_settings(tmp_path, **overrides):
    defaults = dict(
        database_url="postgresql+asyncpg://test:test@localhost:5433/test",
        submit_base_dir=str(tmp_path),
        jobs_per_work_unit=8,
        work_units_per_round=10,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_workflow(**kwargs):
    wf = MagicMock()
    wf.id = kwargs.pop("workflow_id", uuid.uuid4())
    wf.request_name = kwargs.pop("request_name", "test-request-001")
    wf.input_dataset = kwargs.pop("input_dataset", "/TestPrimary/TestProcessed/RECO")
    wf.splitting_algo = kwargs.pop("splitting_algo", "FileBased")
    wf.splitting_params = kwargs.pop("splitting_params", {"files_per_job": 2})
    wf.sandbox_url = kwargs.pop("sandbox_url", "https://example.com/sandbox.tar.gz")
    wf.category_throttles = kwargs.pop("category_throttles", None)
    wf.config_data = kwargs.pop("config_data", {})
    wf.pilot_output_path = kwargs.pop("pilot_output_path", None)
    wf.pilot_cluster_id = kwargs.pop("pilot_cluster_id", None)
    wf.next_first_event = kwargs.pop("next_first_event", 1)
    wf.file_offset = kwargs.pop("file_offset", 0)
    wf.current_round = kwargs.pop("current_round", 0)
    for k, v in kwargs.items():
        setattr(wf, k, v)
    return wf


def _make_planner(tmp_path, **settings_overrides):
    repo = MagicMock()
    dag_row = MagicMock()
    dag_row.id = uuid.uuid4()
    repo.create_dag = AsyncMock(return_value=dag_row)
    repo.update_dag = AsyncMock()
    repo.update_workflow = AsyncMock()
    repo.create_processing_block = AsyncMock()

    settings = _make_settings(tmp_path, **settings_overrides)
    planner = DAGPlanner(
        repository=repo,
        dbs_adapter=MockDBSAdapter(),
        rucio_adapter=MockRucioAdapter(),
        condor_adapter=MockCondorAdapter(),
        settings=settings,
    )
    return planner


# ── GEN node offset tests ────────────────────────────────────────


class TestGenNodesOffset:
    def test_gen_nodes_uses_next_first_event(self, tmp_path):
        """next_first_event=500_001 → nodes start at event 500,001."""
        planner = _make_planner(tmp_path)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 100_000},
            next_first_event=500_001,
        )

        nodes = planner._plan_gen_nodes(wf, wf.config_data)

        # 500,000 remaining events / 100,000 per job = 5 nodes
        assert len(nodes) == 5
        assert nodes[0].first_event == 500_001
        assert nodes[0].last_event == 600_000
        assert nodes[-1].first_event == 900_001
        assert nodes[-1].last_event == 1_000_000

    def test_gen_nodes_max_jobs_caps(self, tmp_path):
        """max_jobs=6, 10 remaining jobs → 6 nodes."""
        planner = _make_planner(tmp_path)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 100_000},
            next_first_event=1,
        )

        nodes = planner._plan_gen_nodes(wf, wf.config_data, max_jobs=6)

        assert len(nodes) == 6
        assert nodes[0].first_event == 1
        assert nodes[-1].first_event == 500_001
        assert nodes[-1].last_event == 600_000

    def test_gen_nodes_all_done_returns_empty(self, tmp_path):
        """next_first_event past total → empty list."""
        planner = _make_planner(tmp_path)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 100_000},
            next_first_event=1_000_001,
        )

        nodes = planner._plan_gen_nodes(wf, wf.config_data)

        assert nodes == []


# ── File-based node offset tests ─────────────────────────────────


class TestFileBasedNodesOffset:
    async def test_file_based_skips_offset(self, tmp_path):
        """file_offset=2, 5 files from DBS → only 3 files split."""
        planner = _make_planner(tmp_path)

        # MockDBSAdapter returns 5 files by default
        wf = _make_workflow(
            splitting_algo="FileBased",
            splitting_params={"files_per_job": 1},
            file_offset=2,
        )

        nodes = await planner._plan_file_based_nodes(wf)

        # 5 files - 2 offset = 3 files, 1 per job = 3 nodes
        assert len(nodes) == 3

    async def test_file_based_max_jobs_caps(self, tmp_path):
        """max_jobs=3 with files_per_job=1 → 3 files processed."""
        planner = _make_planner(tmp_path)

        wf = _make_workflow(
            splitting_algo="FileBased",
            splitting_params={"files_per_job": 1},
            file_offset=0,
        )

        nodes = await planner._plan_file_based_nodes(wf, max_jobs=3)

        assert len(nodes) == 3


# ── Adaptive empty-nodes tests ───────────────────────────────────


class TestAdaptiveEmptyReturnsNone:
    async def test_plan_production_dag_adaptive_empty_returns_none(self, tmp_path):
        """GEN workflow past total events + adaptive=True → returns None (no ValueError)."""
        planner = _make_planner(tmp_path)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 100_000},
            next_first_event=1_000_001,  # Past total → _plan_gen_nodes returns []
        )

        result = await planner.plan_production_dag(wf, adaptive=True)

        assert result is None

    async def test_plan_production_dag_non_adaptive_empty_raises(self, tmp_path):
        """GEN workflow past total events + adaptive=False → ValueError."""
        planner = _make_planner(tmp_path)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 100_000},
            next_first_event=1_000_001,
        )

        with pytest.raises(ValueError, match="No processing nodes"):
            await planner.plan_production_dag(wf, adaptive=False)


# ── Pileup file resolution tests ─────────────────────────────────


class TestPileupResolution:
    async def test_pileup_query_called_for_manifest_steps(self, tmp_path):
        """manifest_steps with mc_pileup → Rucio get_available_pileup_files called."""
        rucio = MockRucioAdapter()
        rucio._pileup_files = ["/store/mc/premix/file1.root", "/store/mc/premix/file2.root"]

        repo = MagicMock()
        dag_row = MagicMock()
        dag_row.id = uuid.uuid4()
        repo.create_dag = AsyncMock(return_value=dag_row)
        repo.update_dag = AsyncMock()
        repo.update_workflow = AsyncMock()
        repo.create_processing_block = AsyncMock()

        settings = _make_settings(tmp_path)
        planner = DAGPlanner(
            repository=repo,
            dbs_adapter=MockDBSAdapter(),
            rucio_adapter=rucio,
            condor_adapter=MockCondorAdapter(),
            settings=settings,
        )

        premix_ds = "/Neutrino/RunIISummer20UL/PREMIX"
        wf = _make_workflow(
            config_data={
                "_is_gen": True,
                "request_num_events": 100_000,
                "manifest_steps": [
                    {"name": "GEN-SIM", "mc_pileup": "", "data_pileup": ""},
                    {"name": "DIGI", "mc_pileup": premix_ds, "data_pileup": ""},
                ],
            },
            splitting_params={"events_per_job": 50_000},
            next_first_event=1,
        )

        await planner.plan_production_dag(wf)

        # Verify Rucio was called for the pileup dataset
        pileup_calls = [c for c in rucio.calls if c[0] == "get_available_pileup_files"]
        assert len(pileup_calls) == 1
        assert pileup_calls[0][1] == (premix_ds,)

    async def test_pileup_files_json_written_to_group_dirs(self, tmp_path):
        """pileup_files.json is written to each merge group directory."""
        rucio = MockRucioAdapter()
        rucio._pileup_files = ["/store/mc/premix/file1.root"]

        repo = MagicMock()
        dag_row = MagicMock()
        dag_row.id = uuid.uuid4()
        repo.create_dag = AsyncMock(return_value=dag_row)
        repo.update_dag = AsyncMock()
        repo.update_workflow = AsyncMock()
        repo.create_processing_block = AsyncMock()

        settings = _make_settings(tmp_path, jobs_per_work_unit=2)
        planner = DAGPlanner(
            repository=repo,
            dbs_adapter=MockDBSAdapter(),
            rucio_adapter=rucio,
            condor_adapter=MockCondorAdapter(),
            settings=settings,
        )

        premix_ds = "/Neutrino/RunIISummer20UL/PREMIX"
        wf = _make_workflow(
            config_data={
                "_is_gen": True,
                "request_num_events": 100_000,
                "manifest_steps": [
                    {"name": "GEN-SIM", "mc_pileup": "", "data_pileup": ""},
                    {"name": "DIGI", "mc_pileup": premix_ds, "data_pileup": ""},
                ],
            },
            splitting_params={"events_per_job": 50_000},
            next_first_event=1,
        )

        await planner.plan_production_dag(wf)

        # Check pileup_files.json was written to the merge group directory
        wf_dir = tmp_path / str(wf.id)
        mg_dir = wf_dir / "mg_000000"
        pileup_path = mg_dir / "pileup_files.json"
        assert pileup_path.exists(), f"Expected {pileup_path} to exist"

        data = json.loads(pileup_path.read_text())
        assert premix_ds in data
        assert data[premix_ds] == ["/store/mc/premix/file1.root"]

    async def test_no_pileup_query_without_manifest_steps(self, tmp_path):
        """No manifest_steps in config → no Rucio pileup query."""
        rucio = MockRucioAdapter()

        repo = MagicMock()
        dag_row = MagicMock()
        dag_row.id = uuid.uuid4()
        repo.create_dag = AsyncMock(return_value=dag_row)
        repo.update_dag = AsyncMock()
        repo.update_workflow = AsyncMock()
        repo.create_processing_block = AsyncMock()

        settings = _make_settings(tmp_path)
        planner = DAGPlanner(
            repository=repo,
            dbs_adapter=MockDBSAdapter(),
            rucio_adapter=rucio,
            condor_adapter=MockCondorAdapter(),
            settings=settings,
        )

        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 100_000},
            splitting_params={"events_per_job": 50_000},
            next_first_event=1,
        )

        await planner.plan_production_dag(wf)

        # No pileup calls should have been made
        pileup_calls = [c for c in rucio.calls if c[0] == "get_available_pileup_files"]
        assert len(pileup_calls) == 0

    async def test_pileup_files_in_transfer_input(self, tmp_path):
        """pileup_files.json appears in processing node submit file transfer_input_files."""
        rucio = MockRucioAdapter()
        rucio._pileup_files = ["/store/mc/premix/file1.root"]

        repo = MagicMock()
        dag_row = MagicMock()
        dag_row.id = uuid.uuid4()
        repo.create_dag = AsyncMock(return_value=dag_row)
        repo.update_dag = AsyncMock()
        repo.update_workflow = AsyncMock()
        repo.create_processing_block = AsyncMock()

        settings = _make_settings(tmp_path, jobs_per_work_unit=2)
        planner = DAGPlanner(
            repository=repo,
            dbs_adapter=MockDBSAdapter(),
            rucio_adapter=rucio,
            condor_adapter=MockCondorAdapter(),
            settings=settings,
        )

        premix_ds = "/Neutrino/RunIISummer20UL/PREMIX"
        wf = _make_workflow(
            config_data={
                "_is_gen": True,
                "request_num_events": 100_000,
                "manifest_steps": [
                    {"name": "DIGI", "mc_pileup": premix_ds, "data_pileup": ""},
                ],
            },
            splitting_params={"events_per_job": 50_000},
            next_first_event=1,
        )

        await planner.plan_production_dag(wf)

        # Read a processing node submit file and check transfer_input_files
        wf_dir = tmp_path / str(wf.id)
        mg_dir = wf_dir / "mg_000000"
        proc_sub = mg_dir / "proc_000000.sub"
        assert proc_sub.exists()
        content = proc_sub.read_text()
        assert "pileup_files.json" in content, (
            "pileup_files.json not in transfer_input_files"
        )

    async def test_duplicate_pileup_dataset_queried_once(self, tmp_path):
        """Same pileup dataset in multiple steps → only one Rucio query."""
        rucio = MockRucioAdapter()
        rucio._pileup_files = ["/store/mc/premix/file1.root"]

        repo = MagicMock()
        dag_row = MagicMock()
        dag_row.id = uuid.uuid4()
        repo.create_dag = AsyncMock(return_value=dag_row)
        repo.update_dag = AsyncMock()
        repo.update_workflow = AsyncMock()
        repo.create_processing_block = AsyncMock()

        settings = _make_settings(tmp_path)
        planner = DAGPlanner(
            repository=repo,
            dbs_adapter=MockDBSAdapter(),
            rucio_adapter=rucio,
            condor_adapter=MockCondorAdapter(),
            settings=settings,
        )

        premix_ds = "/Neutrino/RunIISummer20UL/PREMIX"
        wf = _make_workflow(
            config_data={
                "_is_gen": True,
                "request_num_events": 100_000,
                "manifest_steps": [
                    {"name": "DIGI1", "mc_pileup": premix_ds, "data_pileup": ""},
                    {"name": "DIGI2", "mc_pileup": premix_ds, "data_pileup": ""},
                ],
            },
            splitting_params={"events_per_job": 50_000},
            next_first_event=1,
        )

        await planner.plan_production_dag(wf)

        pileup_calls = [c for c in rucio.calls if c[0] == "get_available_pileup_files"]
        assert len(pileup_calls) == 1, "Same dataset should only be queried once"


# ── Adaptive first_round_work_units tests ─────────────────────────


class TestFirstRoundWorkUnits:
    async def test_round0_adaptive_uses_first_round_work_units(self, tmp_path):
        """Round 0 + adaptive=True → caps to first_round_work_units (1 WU = 8 jobs)."""
        planner = _make_planner(tmp_path, first_round_work_units=1, work_units_per_round=10)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 10_000},
            next_first_event=1,
            current_round=0,
        )

        dag = await planner.plan_production_dag(wf, adaptive=True)

        # 1 WU × 8 jobs_per_work_unit = 8 max jobs
        assert dag is not None
        proc_count = dag.node_counts["processing"] if hasattr(dag, "node_counts") else None
        # The mock returns a MagicMock for the DAG row, so check planner call args
        create_dag_call = planner.db.create_dag.call_args
        node_counts = create_dag_call.kwargs.get("node_counts", {})
        assert node_counts["processing"] == 8

    async def test_round1_adaptive_uses_work_units_per_round(self, tmp_path):
        """Round 1 + adaptive=True → caps to work_units_per_round (10 WUs = 80 jobs)."""
        planner = _make_planner(tmp_path, first_round_work_units=1, work_units_per_round=10)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 10_000_000},
            splitting_params={"events_per_job": 10_000},
            next_first_event=80_001,  # round 0 already processed 80K events
            current_round=1,
        )

        dag = await planner.plan_production_dag(wf, adaptive=True)

        assert dag is not None
        create_dag_call = planner.db.create_dag.call_args
        node_counts = create_dag_call.kwargs.get("node_counts", {})
        assert node_counts["processing"] == 80  # 10 WUs × 8 jobs

    async def test_round0_non_adaptive_no_cap(self, tmp_path):
        """adaptive=False → no cap, all jobs in one DAG."""
        planner = _make_planner(tmp_path, first_round_work_units=1, work_units_per_round=10)
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 500_000},
            splitting_params={"events_per_job": 10_000},
            next_first_event=1,
            current_round=0,
        )

        dag = await planner.plan_production_dag(wf, adaptive=False)

        assert dag is not None
        create_dag_call = planner.db.create_dag.call_args
        node_counts = create_dag_call.kwargs.get("node_counts", {})
        assert node_counts["processing"] == 50  # all 50 jobs, no cap

    async def test_round0_default_first_round_is_1(self, tmp_path):
        """Default first_round_work_units=1 produces 1 WU."""
        planner = _make_planner(tmp_path)  # uses defaults
        wf = _make_workflow(
            config_data={"_is_gen": True, "request_num_events": 1_000_000},
            splitting_params={"events_per_job": 10_000},
            next_first_event=1,
            current_round=0,
        )

        dag = await planner.plan_production_dag(wf, adaptive=True)

        assert dag is not None
        create_dag_call = planner.db.create_dag.call_args
        node_counts = create_dag_call.kwargs.get("node_counts", {})
        # Default first_round_work_units=1 × jobs_per_work_unit=8 = 8 jobs
        assert node_counts["processing"] == 8
