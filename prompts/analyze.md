# BƯỚC 1 — TÁCH KỊCH BẢN ĐỐI THỦ

Đây là kịch bản một video đã viral. Hãy chia nó thành các khúc theo đúng mạch gốc để viết lại.

## Bối cảnh kênh sẽ viết lại
- Tiêu đề video mới: <<TITLE>>
- Ngôn ngữ / khán giả đích: <<LANGUAGE>> — <<AUDIENCE>>
<<TOPIC_GUIDE>>

## Kịch bản đối thủ
<<COMPETITOR_TRANSCRIPT>>

## Yêu cầu
- Giữ nguyên thứ tự mạch gốc.
- Mỗi khúc phải có `competitor_excerpt` là nguyên văn đoạn đối thủ, không tóm tắt.
- Mỗi khúc chỉ ghi những gì cần để viết lại sát bản viral: vai trò, ý/cảm xúc cần giữ, văn phong/nhịp cần bắt chước.
- Không phân tích dài, không thêm framework, không đề xuất mở rộng quá mức.

## Trả về — CHỈ JSON
{
  "why_viral": "vì sao video gốc giữ chân người xem, nói ngắn gọn",
  "emotional_arc": "mạch cảm xúc xuyên suốt, nói ngắn gọn",
  "parts": [
    {
      "id": 1,
      "name": "Tên khúc ngắn gọn",
      "competitor_excerpt": "nguyên văn đoạn đối thủ của khúc này",
      "role": "vai trò khúc trong mạch",
      "keep": "nội dung/cảm xúc chính phải giữ",
      "technique": "văn phong/nhịp/cách kể cần bắt chước"
    }
  ]
}
