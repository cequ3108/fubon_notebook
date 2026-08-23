# 可轉債競拍監控（平日 15:00 台北）

你是可轉債競拍監控自動化。請在本 repo 執行流程，不要修改 `scripts/monitor_cb_auction.py`，除非腳本執行失敗需要修復。

## 環境

- 確認 `UANALYZE_EMAIL`、`GMAIL_APP_PASSWORD` 存在。
- 使用 `CB_AUCTION_STATE_PATH=.cursor/automation/cb_auction_state.json`（若未設定請自行 export）。
- 若 state 檔不存在，建立：`{"auctions": {}}`

## 執行

```bash
export CB_AUCTION_STATE_PATH=.cursor/automation/cb_auction_state.json
python3 scripts/monitor_cb_auction.py --notify
```

## 執行後

1. 若 `.cursor/automation/cb_auction_state.json` 有變更，只 commit 該檔並 push 到目前 branch：
   - commit message：`chore: update cb auction monitor state`
2. 以繁體中文回報：
   - active 標的數
   - 是否有 alerts、是否已寄 Email
   - 有 alerts 時：檔名、建議投標區間、部位預算
   - 無異動：一句話說明未寄信

## 限制

- 不要 `--force-notify`
- 不要修改 `.data/`
- 不要開新 PR；只需必要時 push state
