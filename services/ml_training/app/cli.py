"""Command-line entry point for the ML training service.

::

    python -m app.cli train-lgbm   --experiment cashback_ranker
    python -m app.cli train-svdpp
    python -m app.cli promote      --run-id <run_id>
    python -m app.cli train-all
"""
from __future__ import annotations

import json
import logging
import sys

import click
import structlog

from app.settings import get_settings

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = structlog.get_logger("ml.cli")


# ---------------------------------------------------------------------------
def _ch_client():
    import clickhouse_connect

    cfg = get_settings()
    return clickhouse_connect.get_client(
        host=cfg.clickhouse_host,
        port=cfg.clickhouse_http_port,
        username=cfg.clickhouse_user,
        password=cfg.clickhouse_password,
        database=cfg.clickhouse_db,
    )


# ---------------------------------------------------------------------------
@click.group()
def cli() -> None:
    """ML training entry point."""


@cli.command("train-lgbm")
@click.option("--experiment", default="cashback_ranker", show_default=True)
def train_lgbm_cmd(experiment: str) -> None:
    """Cross-validate + register the LightGBM ranker."""
    from app.data_loader import TrainingDataLoader
    from app.trainer import LGBMTrainer

    cfg = get_settings()
    loader = TrainingDataLoader(
        ch_client=_ch_client(),
        pg_dsn=cfg.postgres_dsn,
        rfm_window_days=cfg.rfm_window_days,
    )
    X, y = loader.build_pairs()

    trainer = LGBMTrainer(mlflow_tracking_uri=cfg.mlflow_tracking_uri)
    run_id = trainer.train(X, y, experiment=experiment)
    click.echo(json.dumps({"run_id": run_id, "model": trainer.registered_model_name}))


@cli.command("train-svdpp")
@click.option("--experiment", default="cashback_retrieval", show_default=True)
def train_svdpp_cmd(experiment: str) -> None:
    """Train ALS retrieval (SVD++) over the (user × MCC) matrix."""
    from app.data_loader import TrainingDataLoader
    from app.svd_trainer import SVDPPTrainer

    cfg = get_settings()
    loader = TrainingDataLoader(
        ch_client=_ch_client(),
        pg_dsn=cfg.postgres_dsn,
        rfm_window_days=cfg.rfm_window_days,
    )
    rfm_df = loader.load_rfm_for_svd()
    if rfm_df.empty:
        click.echo("no RFM data — run trigger-etl first", err=True)
        sys.exit(1)

    trainer = SVDPPTrainer(
        factors=cfg.svd_factors,
        iterations=cfg.svd_iterations,
        mlflow_tracking_uri=cfg.mlflow_tracking_uri,
    )
    run_id = trainer.train(rfm_df, experiment=experiment)
    click.echo(json.dumps({"run_id": run_id, "kind": "svdpp"}))


@cli.command("promote")
@click.option("--run-id", required=True)
def promote_cmd(run_id: str) -> None:
    """Conditionally transition a run to Production."""
    from app.promote_model import promote_if_better

    cfg = get_settings()
    promoted = promote_if_better(run_id, tracking_uri=cfg.mlflow_tracking_uri)
    click.echo(json.dumps({"run_id": run_id, "promoted": bool(promoted)}))
    sys.exit(0 if promoted else 2)


@cli.command("train-all")
@click.option("--experiment", default="cashback_ranker", show_default=True)
def train_all_cmd(experiment: str) -> None:
    """Convenience: train SVD++ + LightGBM + promote in one shot."""
    from app.data_loader import TrainingDataLoader
    from app.promote_model import promote_if_better
    from app.svd_trainer import SVDPPTrainer
    from app.trainer import LGBMTrainer

    cfg = get_settings()
    loader = TrainingDataLoader(
        ch_client=_ch_client(),
        pg_dsn=cfg.postgres_dsn,
        rfm_window_days=cfg.rfm_window_days,
    )

    # ---- SVD++ ---------------------------------------------------------
    rfm_df = loader.load_rfm_for_svd()
    svd_run_id = None
    if not rfm_df.empty:
        svd = SVDPPTrainer(
            factors=cfg.svd_factors,
            iterations=cfg.svd_iterations,
            mlflow_tracking_uri=cfg.mlflow_tracking_uri,
        )
        svd_run_id = svd.train(rfm_df)
        log.info("svdpp_done", run_id=svd_run_id)
    else:
        log.warning("svdpp_skipped_no_rfm")

    # ---- LightGBM ------------------------------------------------------
    X, y = loader.build_pairs()
    trainer = LGBMTrainer(mlflow_tracking_uri=cfg.mlflow_tracking_uri)
    lgbm_run_id = trainer.train(X, y, experiment=experiment)
    log.info("lgbm_done", run_id=lgbm_run_id)

    # ---- Promote -------------------------------------------------------
    promoted = promote_if_better(lgbm_run_id, tracking_uri=cfg.mlflow_tracking_uri)
    click.echo(
        json.dumps(
            {
                "svd_run_id": svd_run_id,
                "lgbm_run_id": lgbm_run_id,
                "promoted": bool(promoted),
            }
        )
    )


if __name__ == "__main__":
    cli()
