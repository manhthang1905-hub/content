# CLAUDE.md — ĐỌC TRƯỚC KHI LÀM BẤT CỨ GÌ

File này dành cho AI (và người) ở mọi session/máy sau. Nó ghi lại **mục tiêu, triết
lý và logic** của tool để bạn KHÔNG đi chệch. Repo này đồng bộ qua GitHub sang nhiều
máy — hãy bám đúng những nguyên tắc dưới đây.

---

## Tool này làm gì
Lấy 1 link video đối thủ đã viral → **remake** thành kịch bản voiceover mới cho kênh
của mình: bám sát đối thủ, viết đúng ngôn ngữ + khán giả của kênh, xuất file cho
ElevenLabs. Chạy hàng loạt theo Google Sheet.

## TRIẾT LÝ CỐT LÕI (quan trọng hơn mọi thứ khác)

1. **ĐƠN GIẢN, DỄ QUẢN LÝ.** Ít file, ít tầng, mỗi thứ ở đúng 1 chỗ dễ tìm. ĐỪNG
   over-engineer, ĐỪNG tách lớp lung tung, ĐỪNG thêm tầng/khái niệm trừu tượng.
   Nếu phân vân giữa "gọn" và "tối ưu kỹ thuật" → chọn GỌN.

2. **REMAKE BÁM SÁT ĐỐI THỦ — KHÔNG tự chế.** Đối thủ đã viral nên đã đúng. Làm Y
   NHƯ nó: giữ ý, mạch, độ dài, nhịp, kiểu CTA. CHỈ đổi: (1) ngôn ngữ, (2) ví dụ/
   cách nói cho hợp khán giả nước đó. **KHÔNG thêm ý, KHÔNG "nâng cấp cho hay hơn",
   KHÔNG tự chế câu cảm xúc, KHÔNG diễn giải dài dòng.** ("Giấy phép tự nâng cấp" là
   thứ làm AI bịa từ vô nghĩa và phình nội dung.)

3. **VIẾT CHO SỐ ĐÔNG.** Mục tiêu cuối: khán giả đại chúng HIỂU NGAY, THẤY MÌNH,
   RUNG ĐỘNG → triệu view. Câu phải hiểu ngay khi nghe, đời thường, gần gũi. TRÁNH
   từ bay bổng, trừu tượng, sách vở, khẩu hiệu cô đọng mơ hồ. **Dễ hiểu quan trọng
   hơn việc bám y câu chữ** — nếu dịch sát mà khó hiểu thì nói lại cho cụ thể.

4. **PROMPT LÀ GỐC, CHECK LÀ NGỌN.** Chất lượng do prompt VIẾT quyết định. Khi content
   chưa hay → sửa `prompts/write.md` hoặc văn phong/insight, ĐỪNG sa vào sửa
   `check.md` hay vọc kỹ thuật (token, code). Đó là sửa ngọn.

5. **KHÔNG VÍ DỤ trong prompt/config — chỉ NGUYÊN TẮC.** Ví dụ làm AI bắt chước y hệt
   (không đa dạng) và không scale qua 10 ngôn ngữ/nhiều topic. Mô tả nguyên tắc, đừng
   đưa câu mẫu.

6. **VOICEOVER nhịp tự nhiên.** Đan xen câu dài–ngắn như đang kể chuyện; ĐỪNG ép mọi
   câu ngắn cụt (băm vụn mất hay). Ưu tiên nghe hay khi đọc lên.

7. **GIỮ "thịt".** Mẩu khoa học/khái niệm của đối thủ (tên nhà nghiên cứu, thuật ngữ)
   làm video đáng tin — GIỮ lại nhưng nói ngắn gọn, đời thường, không thành bài giảng.

8. **LỖI NẶNG NHẤT: sai ngôn ngữ / sai tệp khán giả quốc gia.** Content cho kênh nước
   nào phải đúng ngôn ngữ + chạm đúng tệp khán giả nước đó (xem insight.md).

> Trước khi đổi CẤU TRÚC hoặc thêm tính năng phức tạp: HỎI người dùng. Họ ưu tiên đơn
> giản và kiểm soát được.

## Mô hình tổ chức (rất quan trọng)
- **4 topic** (tâm lý, phát triển bản thân, thú cưng, tài chính), mỗi topic **10 ngôn
  ngữ**, mỗi ngôn ngữ **nhiều tuyến kênh** (TL1, TL2, TL3…) — cùng ngôn ngữ nhưng khác
  văn phong + thời lượng, như "chủ kênh khác nhau". Phân cấp: **TOPIC › NGÔN NGỮ › KÊNH**.
- **Mỗi máy chạy 1 topic** (Sheet riêng + `creds.json` riêng), chọn qua `active_topic`
  trong `config.yaml`. Repo (cấu hình mọi topic) đồng bộ chung qua GitHub; mỗi máy tự
  hoàn thiện topic của nó và push lên để các máy khác có chung.
- Mã kênh trên Sheet: `TLx-Ty` → `TLx` = tuyến văn phong, `Ty` = số ngôn ngữ (bảng
  `languages` trong config.yaml: T1=es, T2=vi, …).

## Cấu trúc thư mục
```
CONTENT/
├── run.py · config.yaml · creds.json · .env · .gitignore · CLAUDE.md · README.md
├── core/                 # ENGINE (code lõi, ít khi đụng)
│   pipeline.py · api.py · sheets.py · fetch.py · youtube.py · checks.py
├── prompts/              # 3 PROMPT CHUNG (gốc của chất lượng): analyze · write · check
├── topics/               # CÂY config kênh: topic › ngôn ngữ › kênh
│   └── {topic}/
│       ├── topic.md      #   lưu ý chủ đề (1 lần)
│       └── {lang}/       #   vd es, vi
│           ├── insight.md    # frontmatter: language; thân: insight tệp khán giả nước đó
│           ├── TL1.md        # KÊNH = văn phong + thời lượng (chủ kênh 1)
│           ├── TL2.md · TL3.md
└── output/               # runtime (gitignore): mỗi job 1 thư mục, chứa cả transcript đối thủ
```
Tìm 1 kênh = đi 1 đường cây `topics/{topic}/{lang}/{TLx}.md` — insight + topic ngay trên đường, không nhảy lung tung.

## Luồng xử lý (core/pipeline.py)
1. **fetch** transcript đối thủ (lưu vào thư mục run của job).
2. **analyze** (`prompts/analyze.md`) → tách đối thủ thành các khúc, mỗi khúc có
   `competitor_excerpt` (nguyên văn) + `keep` (cái cần giữ, gồm thịt khoa học).
3. **write** từng khúc (`prompts/write.md`) bám excerpt → **check** ngay (`prompts/check.md`),
   chưa đạt thì viết lại (tối đa vài lần) → chốt.
4. Ghép + dọn định dạng ElevenLabs (`checks.py`) → `final.txt` → ghi Google Sheet.

## Mở rộng (chỉ thêm file, không sửa code lõi)
- **Thêm tuyến kênh** (vd TL4): tạo `topics/{topic}/{lang}/TL4.md` (văn phong + thời lượng).
- **Thêm ngôn ngữ**: tạo `topics/{topic}/{lang}/` với `insight.md` + các `TLx.md`.
- **Thêm topic / chạy máy mới**: copy repo, tạo `topics/{topic-mới}/…`, đổi
  `active_topic` + `creds.json` + Sheet trong `config.yaml`.

## Chạy
```
python run.py --link "<url>" --channel TL1-T2 --title "<tiêu đề>"   # test 1 link
python run.py --queue [--limit N]                                   # theo Google Sheet
python run.py --ma TL1-0001
```

## ĐỪNG làm (bài học đã trả giá)
- Đừng tách config thành nhiều thư mục song song rời rạc (languages/ riêng, data/
  riêng…) — gom theo cây, mỗi thứ 1 chỗ.
- Đừng để AI "sáng tạo/nâng cấp" vượt đối thủ → sinh nội dung phình, lặp, từ vô nghĩa.
- Đừng nhồi quá nhiều yêu cầu vào 1 prompt → AI viết theo checklist, cứng.
- Đừng đưa ví dụ câu mẫu vào prompt/config.
- Đừng sa vào sửa check / vọc kỹ thuật khi content chưa hay — sửa GỐC (write.md / văn phong / insight).
