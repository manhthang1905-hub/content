# CHECK REMAKE

Hãy chấm như khán giả phổ thông của đúng kênh này. Check chỉ dùng để chọn bản tốt nhất.

Câu hỏi chính: bản mới đã đủ tốt để đi tiếp chưa — giống bản gốc viral về cấu trúc, nội dung, văn phong, cảm xúc; không copy; đúng khán giả kênh?

## Phạm vi check
<<CHECK_SCOPE>>

## Bản gốc đối thủ
<<ORIGINAL_TEXT>>

## Bản mới
<<NEW_TEXT>>

## Kênh cần viết đúng
- Ngôn ngữ/kênh: <<LANGUAGE>>
- Độ dài mục tiêu: khoảng <<TARGET_CHARS>> ký tự
- Khoảng chấp nhận: <<MIN_CHARS>>-<<MAX_CHARS>> ký tự

<<AUDIENCE>>

<<VOICE>>

## Tiêu chí chấm
- Đúng ngôn ngữ, quốc gia, cách gọi và tệp khán giả của kênh.
- Bám bản gốc đã viral: cùng mạch, cùng ý, cùng cảm xúc, cùng kiểu kể; không tự chế hướng mới.
- Không copy câu chữ, không dịch máy sống sượng.
- Không lặp ý, không nói lại điều đã nói, không kéo dài bằng diễn giải vòng.
- Nghe voiceover tự nhiên, có nhịp, có cảm xúc, không đều đều, không dính chữ.
- Cảm xúc không được nặng/kịch hơn bản gốc; phải giữ đúng mức đời thường hay trầm sâu như đoạn đối thủ.
- Giữ người xem muốn nghe tiếp: rõ, gần tai, có câu chốt, không bị mệt.
- Đúng độ dài mục tiêu.
- Nếu là check toàn bài: các khúc nối mượt và script giải quyết đúng lời hứa title/thumb.

`score` là điểm để chọn bản tốt nhất. `why` ghi ngắn vì sao bản này đáng chọn. `fix` chỉ ghi ngắn lý do trừ điểm lớn nhất, không viết dài.

## Trả về — CHỈ JSON
{
  "score": 0,
  "pass": true,
  "why": "",
  "fix": ""
}
