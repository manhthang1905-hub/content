# CONTENT

Link video đối thủ đã viral → **remake** kịch bản voiceover → ElevenLabs. Hàng loạt theo Google Sheet.

> AI/người mới: đọc [CLAUDE.md](CLAUDE.md) trước.

## Chạy
```bash
python core/run.py --link "<url>" --channel TL1-T2 --title "<tiêu đề>"
python core/run.py --queue [--limit N]
python core/run.py --ma TL1-0001
```

## Cấu hình
- `config/config.yaml` — `active_topic`, `languages`, models, Sheet columns
- `config/creds.json` — Google service account (mỗi máy riêng, gitignore)
- `config/.env` — API keys (gitignore)
- `topics/{topic}/{lang}/TLx.md` — văn phong từng kênh (1 dòng + frontmatter)
