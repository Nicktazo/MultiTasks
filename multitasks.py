#!/usr/bin/env python3
"""MultiTasks — lightweight multi-project orchestration system."""

from __future__ import annotations

import argparse
import sys

from core.config import load_config, ConfigError
from core.dag import get_execution_order
from core.runner import capture_git_snapshot
from core.scheduler import run_pipeline, resolve_project_scope
from core.state import RunState, FAILED, build_resume_state


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate config (static checks, optionally workspace checks)."""
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Validation FAILED:\n{e}", file=sys.stderr)
        return 1

    order = get_execution_order(config)
    print(f"Config OK: {len(config.projects)} projects, {len(config.tasks)} tasks")
    print(f"Execution order: {' -> '.join(order)}")

    if args.check_workspace:
        print()
        issues = 0
        for proj in config.projects.values():
            snap = capture_git_snapshot(proj.path)
            if not snap.is_git:
                issues += 1
                print(f"  FAIL  {proj.name}: not a git repository ({proj.path})")
            elif snap.dirty_files:
                issues += 1
                print(f"  warn  {proj.name}: {len(snap.dirty_files)} dirty files")
                for f in snap.dirty_files[:5]:
                    print(f"        {f.strip()}")
                if len(snap.dirty_files) > 5:
                    print(f"        ... and {len(snap.dirty_files) - 5} more")
            else:
                print(f"  ok    {proj.name}: clean")
        if issues:
            print(f"\n{issues} project(s) have workspace issues")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the pipeline."""
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Config error:\n{e}", file=sys.stderr)
        return 1

    scope = None
    if args.project:
        try:
            scope = resolve_project_scope(config, args.project)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Scope: {args.project} + upstream dependencies ({len(scope)} tasks)")

    state = run_pipeline(config, scope=scope)

    failed = [t for t in state.tasks.values() if t.status == FAILED]
    return 1 if failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show status of most recent run."""
    state_dir = "state"
    try:
        config = load_config(args.config)
        state_dir = config.settings.state_dir
    except Exception:
        pass

    state = RunState.latest(state_dir)
    if state is None:
        print("No runs found.")
        return 0

    print(state.summary())
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Retry from the latest run, inheriting only done tasks."""
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Config error:\n{e}", file=sys.stderr)
        return 1

    old_state = RunState.latest(config.settings.state_dir)
    if old_state is None:
        print("No previous run found. Use 'run' instead.", file=sys.stderr)
        return 1

    print(f"Retrying from: {old_state.run_id}")
    old_done = old_state.done_task_ids()
    print(f"Inheriting {len(old_done)} done task(s)")

    # Build task list from current config
    all_order = get_execution_order(config)

    new_state, inherited_done = build_resume_state(
        old_state, all_order, config.settings.state_dir, config=config,
    )

    state = run_pipeline(config, state=new_state, inherited_done=inherited_done)

    failed = [t for t in state.tasks.values() if t.status == FAILED]
    return 1 if failed else 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Start the dashboard in read-only mode."""
    from core.dashboard import start_dashboard

    state_dir = "state"
    port = 18300
    try:
        config = load_config(args.config)
        state_dir = config.settings.state_dir
        port = config.settings.dashboard_port
    except Exception:
        pass

    if hasattr(args, "port") and args.port is not None:
        port = args.port

    run_id = getattr(args, "run_id", None)
    ok = start_dashboard(port=port, state_dir=state_dir, run_id=run_id,
                         config_path=args.config)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="multitasks",
        description="Lightweight multi-project orchestration system",
    )
    parser.add_argument("-c", "--config", default="projects.yaml",
                        help="Config file path (default: projects.yaml)")

    sub = parser.add_subparsers(dest="command")

    # validate
    p_val = sub.add_parser("validate", help="Validate config")
    p_val.add_argument("--check-workspace", action="store_true",
                       help="Also check git workspace status")

    # run
    p_run = sub.add_parser("run", help="Run the pipeline")
    p_run.add_argument("-p", "--project", help="Run only this project + upstream deps")

    # retry
    sub.add_parser("retry", help="Retry from latest run (inherit only done tasks)")

    # status
    sub.add_parser("status", help="Show latest run status")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start dashboard (read-only)")
    p_dash.add_argument("--port", type=int, help="Port (default: from config or 18300)")
    p_dash.add_argument("--run-id", help="Show specific run (default: latest)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "validate": cmd_validate,
        "run": cmd_run,
        "retry": cmd_retry,
        "status": cmd_status,
        "dashboard": cmd_dashboard,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
