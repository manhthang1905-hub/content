# CONTENT — Tool remake kịch bản voiceover đa kênh

Lấy 1 link video đối thủ đã viral → **remake** thành kịch bản mới cho kênh của bạn:
bám sát đối thủ, viết đúng ngôn ngữ + khán giả của kênh, xuất file cho ElevenLabs.

> **AI/người mới vào:** đọc [CLAUDE.md](CLAUDE.md) trước — nó chứa triết lý + logic
> cần bám sát (đơn giản · remake bám đối thủ · viết cho số đông · prompt là gốc).

## Cấu trúc
```
CONTENT/
├── run.py · config.yaml · creds.json · CLAUDE.md
├── core/        # ENGINE (code lõi, ít đụng)
├── prompts/     # 3 prompt CHUNG: analyze · write · check
├── topics/      # CÂY config: topic › ngôn ngữ › kênh
│   └── {topic}/topic.md + {lang}/(insight.md + TL1.md · TL2.md · TL3.md)
└── output/      # runtime: mỗi job 1 thư mục (transcript đối thủ + kịch bản)
```
- `config.yaml`: `active_topic` (máy này chạy topic nào) + bảng `languages` (T1=es…) + model + Sheet.
- Mỗi máy = 1 topic (Sheet + `creds.json` riêng). Repo đồng bộ chung qua GitHub.

## Chạy
```bash
python run.py --link "<url>" --channel TL1-T2 --title "<tiêu đề>"   # test 1 link
python run.py --queue [--limit N]                                   # theo Google Sheet
python run.py --ma TL1-0001
```

## Mở rộng (chỉ thêm file)
- **Thêm tuyến kênh**: `topics/{topic}/{lang}/TL4.md` (văn phong + thời lượng).
- **Thêm ngôn ngữ**: `topics/{topic}/{lang}/` với `insight.md` + các `TLx.md`.
- **Thêm topic / máy mới**: thêm `topics/{topic}/…`, đổi `active_topic` + `creds.json` + Sheet.
