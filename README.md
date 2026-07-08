# Fubon Notebook

富邦新一代（Fubon Neo）Python SDK 的 Jupyter Notebook 工作區，內建 [neoapi-python](https://github.com/phenomenoner/neoapi-skill) skill，供 Cursor 與其他 AI 編碼代理正確使用交易與行情 API。

## 專案結構

```
fubon_notebook/
├── .cursor/skills/neoapi-python/   # vendored skill（v1.0.0-beta.30）
├── notebooks/                      # Jupyter notebooks
├── AGENTS.md                       # Cloud Agent 與多代理入口說明
├── update-skill.sh                 # 從上游同步 skill
└── README.md
```

## Skill 安裝與更新

本專案已將 skill 安裝至 `.cursor/skills/neoapi-python/`。Cursor IDE 與 Cloud Agent 會在啟動時自動載入。

從上游更新：

```bash
./update-skill.sh
# 強制覆蓋（即使版本相同）
FORCE=1 ./update-skill.sh
```

上游 repo：<https://github.com/phenomenoner/neoapi-skill>

## SDK 安裝

Fubon Neo Python SDK **不在 PyPI**，請從官方下載 `.whl`：

- <https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk>

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install /path/to/fubon_neo-*.whl
pip install jupyter ipykernel
python -m ipykernel install --user --name fubon-neo
```

## 測試環境

測試帳號與憑證請透過 Cursor Secrets 設定（見 `AGENTS.md`）。測試環境詳細說明見 `.cursor/skills/neoapi-python/references/test-environment.md`。

## 使用方式

1. 在 Cursor Agent 中提及「富邦 Neo」、「Fubon Neo SDK」等關鍵字，或輸入 `/neoapi-python` 載入 skill。
2. 在 `notebooks/` 建立或編輯 notebook。
3. 參考 `.cursor/skills/neoapi-python/SKILL.md` 中的工作流程與常見錯誤。

## 相關連結

- 官方 API 文件：<https://www.fbs.com.tw/TradeAPI/>
- LLM 友善文件：<https://www.fbs.com.tw/TradeAPI/llms.txt>
- Skill 上游：<https://github.com/phenomenoner/neoapi-skill>
