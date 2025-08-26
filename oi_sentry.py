name: OI Sentry (Binance vs Bybit)

on:
  workflow_dispatch:
  schedule:
    - cron: "*/5 * * * *"   # 5分おき

concurrency:
  group: oi-sentry
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      # 前回の状態ファイルを復元（最も新しいキャッシュを拾う）
      - name: Restore OI state cache
        id: cache-restore
        uses: actions/cache/restore@v4
        with:
          path: oi_state.json
          key: oi-state-${{ github.run_id }}
          restore-keys: |
            oi-state-

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install requests

      - name: Run OI Sentry (single tick)
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
        run: |
          python oi_sentry.py --poll-sec 1 --max-iter 1 --verbose

      # 実行後の状態を新しいキーで保存（次回が最新を拾えるように）
      - name: Save OI state cache
        if: always()
        uses: actions/cache/save@v4
        with:
          path: oi_state.json
          key: oi-state-${{ github.run_id }}
