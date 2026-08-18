# Fubon Notebook — Agent Instructions

本專案用於富邦新一代（Fubon Neo）Python SDK 的 Notebook 開發與實驗。

## Skill 來源

- 上游 repo：[phenomenoner/neoapi-skill](https://github.com/phenomenoner/neoapi-skill)
- 本專案安裝路徑：`.cursor/skills/neoapi-python/`
- 入口檔：`.cursor/skills/neoapi-python/SKILL.md`
- 更新：`./update-skill.sh`（或 `FORCE=1 ./update-skill.sh` 強制覆蓋）

## 必要行為

1. 回覆以繁體中文為主；API 術語與 enum 可附英文。
2. 優先使用線上 `llms.txt` / `llms-full.txt`；bundled 檔案作離線 fallback。
3. 測試環境下單價格上下限以 `sdk.stock.query_symbol_quote` 為準。
4. `intraday.ticker` 查漲跌停；`intraday.quote` 偏成交資料。
5. `get_order_results` 中 status `30` 代表已刪單，仍可能出現在結果中。
6. SDK 不在 PyPI，需從[官方下載頁](https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk)安裝 `.whl`。
7. Python 3.12–3.13；SDK >= 2.2.1 使用 `FubonSDK(30, 2)`。

## Cursor Cloud 環境

### Python / SDK

- 建議 Python 3.12 或 3.13。
- 將官方 `.whl` 放入專案或 VM，以 `pip install <wheel>` 安裝。
- Notebook 執行前先確認 kernel 已安裝 `fubon_neo`。

### 測試環境憑證（Secrets）

透過 Cursor Secrets 設定，勿將憑證寫入 repo：

| 變數 | 說明 |
| :--- | :--- |
| `NEOAPI_TEST_ID` | 測試帳號 ID |
| `NEOAPI_TEST_PASSWORD` | 登入密碼（預設 `12345678`） |
| `NEOAPI_TEST_CERT_PATH` | `.pfx` 憑證路徑 |
| `NEOAPI_TEST_CERT_PASSWORD` | 憑證密碼（預設 `12345678`） |
| `NEOAPI_TEST_URL` | 選填，預設 `wss://neoapitest.fbs.com.tw/TASP/XCPXWS` |

測試環境初始化範例：

```python
import os
from fubon_neo.sdk import FubonSDK

test_url = os.getenv("NEOAPI_TEST_URL", "wss://neoapitest.fbs.com.tw/TASP/XCPXWS")
sdk = FubonSDK(30, 2, url=test_url)
accounts = sdk.login(
    os.environ["NEOAPI_TEST_ID"],
    os.environ["NEOAPI_TEST_PASSWORD"],
    os.environ["NEOAPI_TEST_CERT_PATH"],
    os.environ["NEOAPI_TEST_CERT_PASSWORD"],
)
```

詳細測試環境說明見 `.cursor/skills/neoapi-python/references/test-environment.md`。

## 參考文件

- 官方 LLM 文件：<https://www.fbs.com.tw/TradeAPI/docs/welcome/build-with-llm>
- Skill 文件索引：`.cursor/skills/neoapi-python/references/doc-index.md`
