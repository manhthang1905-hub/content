# CLAUDE.md

Mục tiêu, triết lý và cấu trúc tool — AI/người mới vào đọc trước để không đi chệch.

---

## Tool làm gì
Link video đối thủ đã viral → **remake** kịch bản voiceover cho kênh: đúng ngôn ngữ + văn phong → xuất file cho ElevenLabs. Chạy hàng loạt theo Google Sheet.

## Triết lý

1. **ĐƠN GIẢN.** Ít file, ít tầng, mỗi thứ 1 chỗ. Phân vân "gọn" vs "tối ưu kỹ thuật" → chọn GỌN.

2. **REMAKE BÁM ĐỐI THỦ — KHÔNG tự chế.** Giữ y: ý, mạch, độ dài, nhịp, CTA. Chỉ đổi ngôn ngữ + cách nói cho hợp khán giả. KHÔNG thêm ý, KHÔNG "nâng cấp".

3. **VIẾT CHO SỐ ĐÔNG.** Câu phải hiểu ngay khi nghe, đời thường, gần gũi. Dễ hiểu quan trọng hơn bám y câu chữ.

4. **PROMPT LÀ GỐC.** Content chưa hay → sửa `prompts/write_oneshot.md` hoặc `TLx.md`. Đừng sa vào sửa check hay vọc code.

5. **KHÔNG ví dụ trong prompt/config — chỉ nguyên tắc.** Ví dụ làm AI bắt chước y hệt, không scale được qua 10 ngôn ngữ.

6. **VOICEOVER nhịp tự nhiên.** Đan xen câu dài–ngắn như kể chuyện. Nghe hay khi đọc lên.

7. **Giữ "thịt" khoa học.** Tên nghiên cứu, thuật ngữ của đối thủ làm video đáng tin — giữ nhưng nói ngắn, đời thường.

> Trước khi đổi cấu trúc hoặc thêm tính năng: HỎI người dùng trước.

## Cấu trúc
```
CONTENT/
├── CLAUDE.md · README.md · .gitignore · gui.py
├── config/           # config.yaml · creds.json · .env
├── core/             # pipeline.py · api.py · sheets.py · youtube.py · run.py · backfill.py
├── prompts/          # title_thumb.md · write_oneshot.md · check_fix.md
├── topics/           # CÂY kênh: {topic}/{lang}/TLx.md
└── output/           # runtime (gitignore)
```

- `TLx.md` = frontmatter (`language`, `thumb_case`, `title_thumb`) + **1 dòng** mô tả văn phong.
- Mã kênh `TLx-Ty`: `TLx` = tuyến văn phong, `Ty` = ngôn ngữ (bảng `languages` trong config.yaml).
- Mỗi máy chạy 1 topic (`active_topic` trong config.yaml). Repo sync qua GitHub.

## Pipeline (core/pipeline.py)
1. **fetch** — lấy transcript đối thủ
2. **title_thumb** — tạo tiêu đề + text thumbnail
3. **write_oneshot** — viết toàn bộ kịch bản bám transcript đối thủ
4. **check_fix** — so sánh với đối thủ, sửa nếu chưa đạt
5. format + lưu `final.txt` → ghi Sheet

## Mở rộng (chỉ thêm file)
- **Thêm tuyến kênh**: tạo `topics/{topic}/{lang}/TL4.md`
- **Thêm ngôn ngữ**: tạo `topics/{topic}/{lang}/` với các `TLx.md`
- **Thêm topic / máy mới**: thêm `topics/{topic}/…`, đổi `active_topic` + `creds.json` + Sheet

## Chạy
```
python core/run.py --link "<url>" --channel TL1-T2 --title "<tiêu đề>"
python core/run.py --queue [--limit N]
python core/run.py --ma TL1-0001
```

## Đừng làm
- Đừng để AI "nâng cấp" vượt đối thủ → nội dung phình, lặp, vô nghĩa.
- Đừng nhồi quá nhiều yêu cầu vào 1 prompt → AI viết theo checklist, cứng.
- Đừng đưa ví dụ câu mẫu vào prompt/config.
- Đừng sửa check_fix khi content chưa hay — sửa write_oneshot.md hoặc TLx.md.
