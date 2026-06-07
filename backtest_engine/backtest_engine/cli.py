import asyncio
import click
import csv
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import subprocess
import uuid

from backtest_engine.db import get_db, DATABASE_URL, assert_backtest_db_is_isolated
from backtest_engine.source_db import get_source_db, SOURCE_DATABASE_URL
from backtest_engine.models import BacktestArtifact, BacktestRun
from backtest_engine.ea_config import generate_dict_hash, load_ea_config
from backtest_engine.broker_specs import (
    apply_commission_overrides,
    get_broker_specs_hash,
    load_broker_specs,
)
from backtest_engine.simulation.strategy_loader import load_strategies
from backtest_engine.simulation.candles import load_cagg_candles, load_m1_candles, load_raw_broker_candles
from backtest_engine.simulation.state import simulate_strategy
from backtest_engine.reporting.markdown import (
    build_standard_summaries,
    generate_markdown_report,
    generate_summary_report,
)
from backtest_engine.reporting.advanced_insights import generate_advanced_trade_insights
from backtest_engine.reporting.csv_export import export_frame_to_csv, export_results_to_csv
from backtest_engine.reporting.aggregates import results_to_frame
from backtest_engine.reporting.plots import generate_plots
from backtest_engine.sweeps import load_sweep_variants, merge_config, validate_management_config

@click.group()
def cli():
    """PipFactor Backtest Engine CLI."""
    pass

@cli.group()
def db():
    """Database management commands."""
    pass

@db.command(name="upgrade")
def db_upgrade():
    """Run Alembic upgrades on the backtest database."""
    print("Running alembic upgrade head...")
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    subprocess.run(["alembic", "upgrade", "head"], check=True, env=env)

@db.command(name="dump")
@click.option('--out', default='backtest_lab.pgdump', help='Output file for the dump')
def db_dump(out):
    """Dump the backtest database."""
    print(f"Dumping database to {out}...")
    import urllib.parse
    url = urllib.parse.urlparse(DATABASE_URL)
    env = os.environ.copy()
    if url.password:
        env['PGPASSWORD'] = url.password
    cmd = [
        'pg_dump',
        '-h', url.hostname or 'localhost',
        '-p', str(url.port or 5432),
        '-U', url.username or 'postgres',
        '-F', 'c',
        '-f', out,
        url.path.lstrip('/')
    ]
    subprocess.run(cmd, env=env, check=True)

@cli.command(name="run")
@click.option('--profile', default='ea_v3_strict', help='Backtest profile name')
@click.option('--universe', default='expired', help='Strategy universe to filter by')
@click.option('--strategy-id', type=int, help='Filter by a specific strategy ID')
@click.option('--symbol', help='Filter by symbol')
@click.option('--from', 'from_date', help='Start date (ISO format)')
@click.option('--to', 'to_date', help='End date (ISO format)')
@click.option('--limit', type=int, help='Maximum number of strategies to run')
@click.option('--reuse/--no-reuse', default=True, help='Reuse cached data where possible')
@click.option('--dry-run', is_flag=True, help='Run without committing to DB')
@click.option('--report-dir', default='/tmp/backtests', help='Directory to save reports')
@click.option('--fail-on-missing-specs', is_flag=True, help='Fail if a broker spec is missing')
@click.option('--ea-config', default='configs/ea_v3_00.yml', help='Path to EA config YAML')
@click.option('--broker-specs', default='configs/pipfactor_broker_specs_20260529_195422.jsonl', help='Path to broker specs JSONL')
@click.option('--replay-until', help='Post-entry candle replay end time (ISO). Defaults to latest available loaded data.')
@click.option('--max-trade-horizon-days', default=7, type=int, help='If --replay-until is absent, load candles through expiry plus this many days. Use 0 for no cap.')
@click.option('--sweep-config', help='YAML file with named EA config variants')
@click.option('--variant', help='Only run one variant id from --sweep-config')
@click.option('--sweep-id', help='Optional id to group sweep runs')
@click.option('--max-combinations', default=24, type=int, help='Safety cap for sweep variants')
def run_command(
    profile, universe, strategy_id, symbol, from_date, to_date, limit,
    reuse, dry_run, report_dir, fail_on_missing_specs, ea_config, broker_specs,
    replay_until, max_trade_horizon_days, sweep_config, variant, sweep_id, max_combinations
):
    """Run a backtest simulation."""
    asyncio.run(run_backtest_async(
        profile_name=profile,
        universe=universe,
        strategy_id=strategy_id,
        symbol=symbol,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        reuse=reuse,
        dry_run=dry_run,
        fail_on_missing_specs=fail_on_missing_specs,
        ea_config_path=ea_config,
        broker_specs_path=broker_specs,
        output_dir=report_dir,
        replay_until=replay_until,
        max_trade_horizon_days=max_trade_horizon_days,
        sweep_config_path=sweep_config,
        variant=variant,
        sweep_id=sweep_id,
        max_combinations=max_combinations,
    ))


async def run_backtest_async(
    profile_name: str,
    universe: str,
    strategy_id: int | None,
    symbol: str | None,
    from_date: str | None,
    to_date: str | None,
    limit: int | None,
    reuse: bool,
    dry_run: bool,
    fail_on_missing_specs: bool,
    ea_config_path: str,
    broker_specs_path: str,
    output_dir: str,
    replay_until: str | None = None,
    max_trade_horizon_days: int = 7,
    sweep_config_path: str | None = None,
    variant: str | None = None,
    sweep_id: str | None = None,
    max_combinations: int = 24,
):
    assert_backtest_db_is_isolated(SOURCE_DATABASE_URL)

    base_config = load_ea_config(ea_config_path)
    broker_hash = get_broker_specs_hash(broker_specs_path)

    variants = [None]
    if sweep_config_path:
        variants = load_sweep_variants(sweep_config_path)
        if variant:
            variants = [v for v in variants if v.variant_id == variant]
            if not variants:
                raise RuntimeError(f"Variant not found in sweep config: {variant}")
        if len(variants) > max_combinations:
            raise RuntimeError(f"Sweep has {len(variants)} variants, exceeding --max-combinations={max_combinations}")
        sweep_id = sweep_id or datetime.now(timezone.utc).strftime("sweep-%Y%m%d%H%M%S")

    sweep_rows = []

    sweep_rows = []
    
    sem = asyncio.Semaphore(4)
    
    async def run_variant(sweep_variant):
        async with sem:
            ea_cfg = base_config
            variant_id = None
            variant_overrides = {}
            if sweep_variant is not None:
                variant_id = sweep_variant.variant_id
                variant_overrides = sweep_variant.overrides
                ea_cfg = merge_config(base_config, variant_overrides)
            validate_management_config(ea_cfg)

            return await _run_single_backtest(
                profile_name=profile_name if variant_id is None else f"{profile_name}_{variant_id}",
                universe=universe,
                strategy_id=strategy_id,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                dry_run=dry_run,
                fail_on_missing_specs=fail_on_missing_specs,
                ea_config=ea_cfg,
                ea_hash=generate_dict_hash(ea_cfg),
                broker_hash=broker_hash,
                broker_specs_path=broker_specs_path,
                output_dir=output_dir,
                replay_until=replay_until,
                max_trade_horizon_days=max_trade_horizon_days,
                sweep_id=sweep_id,
                variant_id=variant_id,
                variant_description=getattr(sweep_variant, "description", "") if sweep_variant else "",
                variant_overrides=variant_overrides,
            )

    sweep_rows = await asyncio.gather(*(run_variant(v) for v in variants))

    if sweep_config_path and sweep_id:
        _write_sweep_summary(Path(output_dir), sweep_id, sweep_rows)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "run"


def _run_output_dirs(output_dir: str, run: BacktestRun, sweep_id: str | None, variant_id: str | None) -> dict[str, Path]:
    base = Path(output_dir)
    if sweep_id:
        root = base / "sweeps" / _slug(sweep_id) / _slug(variant_id or run.run_name or str(run.run_id))
    else:
        timestamp = run.started_at.strftime("%Y%m%d_%H%M%S")
        root = base / f"{_slug(run.profile_name)}_{timestamp}"

    dirs = {
        "root": root,
        "raw": root / "raw",
        "summaries": root / "summaries",
        "plots": root / "plots",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _result_summary_row(run: BacktestRun, results: list, variant_id: str | None, variant_description: str) -> dict:
    df = results_to_frame(results)
    return {
        "run_id": str(run.run_id),
        "run_name": run.run_name,
        "variant_id": variant_id or "",
        "variant_description": variant_description,
        "total_strategies": run.total_strategies,
        "processed_strategies": run.processed_strategies,
        "entered_trades": run.executed_trades,
        "no_trade_count": run.no_trade_count,
        "unsupported_count": run.unsupported_count,
        "closed_tp": int((df["outcome"] == "closed_tp").sum()) if not df.empty else 0,
        "closed_sl": int((df["outcome"] == "closed_sl").sum()) if not df.empty else 0,
        "closed_trailing_sl": int((df["outcome"] == "closed_trailing_sl").sum()) if not df.empty else 0,
        "open_at_data_end": int((df["outcome"] == "open_at_data_end").sum()) if not df.empty else 0,
        "partials": int(df["partial_close_executed"].sum()) if not df.empty else 0,
        "be_moves": int(df["break_even_moved"].sum()) if not df.empty else 0,
        "net_pnl": round(float(df["net_pnl"].sum()), 2) if not df.empty else 0.0,
    }


def _write_sweep_summary(output_dir: Path, sweep_id: str, rows: list[dict]) -> None:
    sweep_dir = output_dir / "sweeps" / _slug(sweep_id)
    sweep_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sweep_dir / "sweep_summary.csv"
    md_path = sweep_dir / "sweep_summary.md"

    if not rows:
        md_path.write_text("# Sweep Summary\n\nNo variants ran.\n")
        return

    fieldnames = list(rows[0].keys())
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    sorted_rows = sorted(rows, key=lambda row: row["net_pnl"], reverse=True)
    lines = [
        "# Sweep Summary",
        "",
        f"Sweep ID: `{sweep_id}`",
        "",
        "| Variant | Run ID | Entered | TP | SL | Trailing SL | Net PnL |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in sorted_rows:
        lines.append(
            f"| {row['variant_id']} | `{row['run_id']}` | {row['entered_trades']} | "
            f"{row['closed_tp']} | {row['closed_sl']} | {row['closed_trailing_sl']} | {row['net_pnl']:.2f} |"
        )
    md_path.write_text("\n".join(lines) + "\n")


async def _run_single_backtest(
    profile_name: str,
    universe: str,
    strategy_id: int | None,
    symbol: str | None,
    from_date: str | None,
    to_date: str | None,
    limit: int | None,
    dry_run: bool,
    fail_on_missing_specs: bool,
    ea_config: dict,
    ea_hash: str,
    broker_hash: str,
    broker_specs_path: str,
    output_dir: str,
    replay_until: str | None,
    max_trade_horizon_days: int,
    sweep_id: str | None,
    variant_id: str | None,
    variant_description: str,
    variant_overrides: dict,
) -> dict:
    print(f"Starting backtest run: {profile_name}")
    broker_specs = apply_commission_overrides(load_broker_specs(broker_specs_path), ea_config)
    try:
        trade_executor_path = Path(__file__).parent.parent.parent.parent / "MT5_stuff" / "TradeExecutor.mq5"
        with open(trade_executor_path, 'rb') as f:
            trade_executor_hash = hashlib.sha256(f.read()).hexdigest()
    except Exception:
        trade_executor_hash = "unknown"

    source_db_gen = get_source_db()
    source_session = await anext(source_db_gen)
    
    dest_db_gen = get_db()
    dest_session = await anext(dest_db_gen)
    
    start_time = datetime.now(timezone.utc)
    parsed_from = _parse_dt(from_date)
    parsed_to = _parse_dt(to_date)
    parsed_replay_until = _parse_dt(replay_until)
    
    import urllib.parse
    source_url = urllib.parse.urlparse(SOURCE_DATABASE_URL)
    source_db_name = source_url.path.lstrip('/')
    
    run_id = uuid.uuid4()
    run = BacktestRun(
        run_id=run_id,
        run_name=f"{profile_name}-{start_time.strftime('%Y%m%d%H%M%S')}",
        profile_name=profile_name,
        profile_version="1.0",
        engine_version="1.0",
        source_database_name=source_db_name,
        source_database_fingerprint={},
        strategy_filter={
            "universe": universe,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "from_date": from_date,
            "to_date": to_date,
            "limit": limit,
            "replay_until": replay_until,
            "sweep_id": sweep_id,
            "variant_id": variant_id,
            "variant_overrides": variant_overrides,
        },
        ea_config=ea_config,
        ea_config_hash=ea_hash,
        broker_specs_hash=broker_hash,
        trade_executor_hash=trade_executor_hash,
        fill_model="m1_ohlc_conservative",
        status="running",
        started_at=start_time
    )
    
    if not dry_run:
        dest_session.add(run)
        await dest_session.flush()
    
    # Load strategies with filters
    strategies = await load_strategies(
        source_session,
        limit=limit,
        symbol=symbol,
        strategy_id=strategy_id,
        universe=universe,
        from_date=parsed_from,
        to_date=parsed_to,
    )
    
    if limit is not None:
        strategies = strategies[:limit]
        
    run.total_strategies = len(strategies)
    print(f"Loaded {len(strategies)} strategies from source DB.")
    
    results = []
    executed_count = 0
    no_trade_count = 0
    unsupported_count = 0
    
    for strategy in strategies:
        print(f"Simulating Strategy {strategy.strategy_id} - {strategy.symbol} {strategy.direction}")
        
        spec = broker_specs.get(strategy.symbol)
        if not spec:
            print(f"Symbol {strategy.symbol} not found in broker specs.")
            if fail_on_missing_specs:
                raise RuntimeError(f"Missing broker spec for {strategy.symbol}")
            continue
            
        timeframe = strategy.entry_signal.get('timeframe', 'H1') if strategy.entry_signal else 'H1'
        if parsed_replay_until is not None:
            candle_end = parsed_replay_until
        elif max_trade_horizon_days > 0:
            candle_end = strategy.expiry_time + timedelta(days=max_trade_horizon_days)
        else:
            candle_end = None

        normalized_timeframe = str(timeframe or "H1").upper()
        if normalized_timeframe == "M1":
            cagg_df = await load_m1_candles(source_session, strategy.symbol, strategy.timestamp, candle_end)
        elif normalized_timeframe in {"D1", "W1", "MN1"}:
            cagg_df = await load_raw_broker_candles(source_session, strategy.symbol, normalized_timeframe, strategy.timestamp, candle_end)
        else:
            cagg_df = await load_cagg_candles(source_session, strategy.symbol, normalized_timeframe, strategy.timestamp, candle_end)
            
        m1_df = await load_m1_candles(source_session, strategy.symbol, strategy.timestamp, candle_end)
        
        # This will need to be updated to match the new BacktestResult schema
        # For now, we mock the call and response mapping
        try:
            res = await simulate_strategy(
                strategy=strategy,
                cagg_df=cagg_df,
                m1_df=m1_df,
                ea_config=ea_config,
                broker_spec=spec,
                run_id=run.run_id,
                current_balance=500.0 # dummy
            )
            results.append(res)
            # Update metrics based on PRDS outcome vocabulary
            if getattr(res, 'entry_time', None) is not None:
                executed_count += 1
            if getattr(res, 'outcome', '') == "unsupported_condition_type":
                unsupported_count += 1
            if getattr(res, 'entry_time', None) is None and getattr(res, 'outcome', '') != "unsupported_condition_type":
                no_trade_count += 1
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Simulation failed for {strategy.strategy_id}: {e}")
            
    run.finished_at = datetime.now(timezone.utc)
    run.processed_strategies = len(results)
    run.executed_trades = executed_count
    run.no_trade_count = no_trade_count
    run.unsupported_count = unsupported_count
    run.status = "completed"
    
    if not dry_run:
        dest_session.add_all(results)
        await dest_session.commit()
        print("Backtest results committed to database.")
    else:
        print("Dry run completed. Nothing saved to database.")
    
    dirs = _run_output_dirs(output_dir, run, sweep_id, variant_id)
    report_md = generate_markdown_report(run, results)
    artifacts: list[Path] = []
    
    report_path = dirs["root"] / "README.md"
    with open(report_path, "w") as f:
        f.write(report_md)
    artifacts.append(report_path)
        
    results_path = dirs["raw"] / "results.csv"
    export_results_to_csv(results, results_path)
    artifacts.append(results_path)

    summaries = build_standard_summaries(results)
    title_cols = {
        "per_symbol": ["symbol"],
        "daily": ["period"],
        "weekly": ["period"],
        "monthly": ["period"],
        "symbol_daily": ["symbol", "period"],
        "symbol_weekly": ["symbol", "period"],
        "symbol_monthly": ["symbol", "period"],
    }
    for name, frame in summaries.items():
        csv_path = dirs["summaries"] / f"{name}.csv"
        md_path = dirs["summaries"] / f"{name}.md"
        export_frame_to_csv(frame, csv_path)
        md_path.write_text(generate_summary_report(name.replace("_", " ").title(), frame, title_cols[name]))
        artifacts.extend([csv_path, md_path])

    if not dry_run:
        advanced_insights = await generate_advanced_trade_insights(
            dest_session=dest_session,
            source_session=source_session,
            run_id=run.run_id,
            broker_specs=broker_specs,
            output_dir=dirs["summaries"] / "advanced_insights",
        )
        artifacts.extend(advanced_insights.output_paths)

    artifacts.extend(generate_plots(results, dirs["plots"], str(run.run_id)))

    if not dry_run:
        for path in artifacts:
            dest_session.add(
                BacktestArtifact(
                    run_id=run.run_id,
                    artifact_type=f"{path.parent.name}_{path.suffix.lstrip('.') or 'file'}",
                    path=str(path),
                    sha256=_file_sha256(path),
                )
            )
        await dest_session.commit()

    print(f"Reports written to {dirs['root']}")
    
    await source_session.close()
    await dest_session.close()

    return _result_summary_row(run, results, variant_id, variant_description)

if __name__ == '__main__':
    cli()
