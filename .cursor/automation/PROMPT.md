# TWSE 競拍（可轉債+股票） + 股票公開申購監控（平日 15:00 台北）

你是台股申購／競拍監控自動化。請在本 repo 依序執行下列流程；
不要修改監控腳本，除非執行失敗需要修復。

## 環境

- 確認 `UANALYZE_EMAIL`、`GMAIL_APP_PASSWORD` 存在。
- 競拍 state：`CB_AUCTION_STATE_PATH=.cursor/automation/cb_auction_state.json`
- 股票申購 state：`STOCK_SUB_STATE_PATH=.cursor/automation/stock_subscription_state.json`
- 收件人清單：`.cursor/automation/alert_recipients.txt`（逐一寄送、互不顯示）
- 若 state 檔不存在，建立對應空結構：
  - 競拍：`{"auctions": {}}`
  - 股票申購：`{"subscriptions": {}}`

## 執行

`monitor_cb_auction.py` 會從證交所競價拍賣公告抓「可轉債 + 股票競拍」，
並在結束後自動接續跑股票公開申購監控
（可用 `SKIP_STOCK_SUB_MONITOR=1` 關閉）。

```bash
export CB_AUCTION_STATE_PATH=.cursor/automation/cb_auction_state.json
export STOCK_SUB_STATE_PATH=.cursor/automation/stock_subscription_state.json

python3 scripts/monitor_cb_auction.py --notify
```

## 執行後

1. 若下列檔案有變更，只 commit 這些檔並 push 到目前 branch：
   - `.cursor/automation/cb_auction_state.json`
   - `.cursor/automation/stock_subscription_state.json`
   - commit message：`chore: update auction and subscription monitor state`
2. 以繁體中文回報：
   - 競拍：active 數（可轉債／股票分開）、alerts、是否寄信
   - 股票申購：active 數、alerts、是否寄信
   - 有 alerts 時列出檔名與關鍵數字（投標區間／興櫃折價／報酬率）
   - 無異動：一句話說明未寄信

## 限制

- 不要 `--force-notify`
- 不要修改 `.data/`
- 不要開新 PR；只需必要時 push state
