import argparse

import optuna
from backtest import load_history, run_backtest
from config import GridConfig
from metrics import summarize_equity_curve


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--trials", type=int, default=30)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    history = load_history(args.days, args.csv)

    def objective(trial: optuna.Trial) -> float:
        config = GridConfig(
            spacing_pct=trial.suggest_float("spacing_pct", 0.0015, 0.008),
            levels_per_side=trial.suggest_int("levels_per_side", 2, 6),
            per_level_notional_usd=trial.suggest_float(
                "per_level_notional_usd", 1000.0, 4000.0, step=250.0
            ),
            max_position_notional_usd=trial.suggest_float(
                "max_position_notional_usd", 6000.0, 20000.0, step=1000.0
            ),
            refresh_threshold_pct=trial.suggest_float(
                "refresh_threshold_pct", 0.001, 0.01
            ),
        )
        equity_curve, _ = run_backtest(config, history)
        metrics = summarize_equity_curve(equity_curve)
        # todo: what is the competition metric?
        return (
            metrics["calmar"] + 0.25 * metrics["sharpe"] + 0.1 * metrics["total_return"]
        )

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=args.trials)
    print({"best_value": study.best_value, "best_params": study.best_params})
