# Evaluation Report

> Báo cáo đánh giá chất lượng sản phẩm theo tiêu chí BTC.

Cập nhật 2026-10-17. Tổng hợp từ [`ROUTER_STRENGTHS.md`](ROUTER_STRENGTHS.md) và [`ROUTER_LIMITATIONS.md`](ROUTER_LIMITATIONS.md) (số liệu gốc chốt ngày 2026-10-12). Đối tượng đánh giá là router: jev chấm điểm prompt → TierPolicy chọn tier (small / mid / large) → chọn model trong pool. Số liệu chi tiết nằm trong [`BENCHMARK_SUMMARY.md`](BENCHMARK_SUMMARY.md) và [`router_hard_benchmark_20261011.md`](router_hard_benchmark_20261011.md).

---

## 1. Metrics

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Response accuracy | >80% | **85,5%** (141/165 câu tiếng Việt thường); **97,9%** (94/96 câu, có đề thi toán khó) | ✅ |
| Response latency | <3s | Bước định tuyến: jev p50 **0,27 s**, p95 0,55 s. Cả request: p50 **11,7 s**, p95 39 s (pool proxy3) | ⚠️ Định tuyến đạt, cả request chưa đạt |
| User satisfaction | >4/5 | Chưa thu thập | ⏳ |
| Test coverage | >60% | Chưa đo | ⏳ |

Ghi chú:
- Độ chính xác đạt mục tiêu, nhưng trên đề thường **luôn dùng small cũng đúng 141/165**, nên con số này chưa chứng minh router có ích hơn cấu hình rẻ nhất (xem mục 2.3).
- Độ trễ cả request chủ yếu do model trả lời (đặc biệt ở effort high), không do bước định tuyến. Định tuyến cộng thêm khoảng 0,7 s (hiểu prompt + tách task) và ~$0,00016 mỗi request.

## 2. Test Results

### 2.1 Tóm tắt

| Bộ đo | Router | So sánh | Kết luận |
|---|---|---|---|
| `harder_cases` (96 câu, 66 câu thi toán khó) | **94/96**, $2,13 | Luôn small 65/96; luôn Sol effort low 88/96, $1,02 | Đúng nhiều nhất, nhưng tốn gấp ~2 lần |
| `hard_cases` (100 câu) | 97–98/100, $0,49–0,67 | Luôn small 84; luôn Sol low 95, $0,41 | Đúng hơn, nhưng đắt hơn cả luôn large |
| Answer bench (165 câu tiếng Việt, pool proxy3) | 141/165, ~$0,174 thực tế | Luôn small 141/165, $0,107 | Ngang small, tốn ~1,6 lần, p95 chậm 2,8 lần |
| Answer bench (165 câu, pool GPT, có tách task) | 143/165, $0,98 | Luôn small 137, $0,20; luôn mid 142, $0,80 | Hơn 6 câu với chi phí gấp ~5 lần |
| Đồng thuận tiếng Việt (450 câu) | Balanced **86,9%** | Luôn small 50%; luật cũ 81,1% (p = 0,020) | Chọn tier tốt hơn có ý nghĩa thống kê |
| Đồng thuận + hard (510 câu) | Recall large **96%**, balanced 88,1% | Luật cũ 76,2% | Không bỏ sót câu cần large |
| jev benchmark (60 case) | **90%** đúng tier (98% nếu dùng điểm gán tay) | | Luật gần như không sai, lỗi chủ yếu từ jev |
| Context bench (38 case) | 79% đúng; 100% ở code, injection, live data, clarify | Luật cũ ngang | Chịu được request giống thật, yếu ở rag_context |
| Routing benchmark (22 model) | **100%** đúng tier, $0,57/1M token (nhãn đề xuất) | Nhãn hiện tại 78%, $0,93 | Cần áp dụng bộ nhãn catalog mới |
| RouterArena (763 câu, tiếng Anh) | 79,3% chất lượng với 10% chi phí; recall large **1%** | Chia ngẫu nhiên cùng chi phí 78,5%; luôn mid 89,5% | Gần như không hơn ngẫu nhiên |

### 2.2 Điểm mạnh

**Đề khó: router đúng nhiều nhất.** Trên `harder_cases` (Apex 2025, AIME 2026, HMMT 2025–2026, Omni-MATH):

| | Luôn small | Luôn Sol effort low | **Router** |
|---|---|---|---|
| Câu dễ (30) | 29 | 29 | 28 |
| Câu khó (66) | 36 | 59 | **66** |
| — Apex (12) | **0** | 8 | **12** |
| **Tổng** | **65** | **88** | **94** |

- Hơn luôn small **29 câu**, lớn hơn nhiều mức nhiễu giữa hai lượt chạy (9–17/165).
- Có 7 câu chỉ effort high làm được (đã đối chiếu tay); router gửi cả 7 lên effort high và làm đúng cả 7.
- Tách câu dễ/khó gần như tuyệt đối: giữ 29/30 câu dễ ở small (tốn $0,007), nâng tier 66/66 câu khó.
- Trên `hard_cases`: small sai 16 câu, router sửa được 15; cả 14/14 câu AIME/HMMT small làm sai đều được gửi lên model mạnh và làm đúng.

**Chọn tier trên câu tiếng Việt thật** (nhãn người và GPT gán trùng, chưa dùng để chỉnh luật):

| Phương án | Đúng tier | Recall mid | Balanced | Điểm phạt |
|---|---|---|---|---|
| Luôn small | 80,0% | 0% | 50,0% | 0,600 |
| Luật cũ | 88,4% | 69% | 81,1% | 0,218 |
| Mô hình học (shadow) | 87,1% | 78% | 83,6% | 0,196 |
| **Router hiện tại** | **91,8%** | **79%** | **86,9%** | **0,151** |

LiveBench olympiad: 10/10 câu lên large (luật cũ 4/10).

**Các thành phần riêng lẻ:**
- TierPolicy trên điểm gán tay: 98% đúng tier, 0% chọn đắt hơn mức cần. Điểm jev lệch tối đa ±1 so với nhãn tay ở 93–98% case (MAE 0,24–0,36 trên thang 0–3).
- Prompt injection kiểu "hãy dùng model rẻ nhất" không làm đổi lựa chọn của router.
- Chọn model từ tier: 100% đúng tier và rẻ hơn ($0,57 so với $0,93 mỗi 1M token; $0,27 với `cost_first`).

### 2.3 Hạn chế

**Đề thường: ngang luôn small nhưng tốn hơn.** 165 câu tiếng Việt, pool proxy3 (flash-lite / v4-flash / v4-pro):

| Cấu hình | Đúng | p50 / p95 | Chi phí thực tế (ước tính) |
|---|---|---|---|
| Luôn small | **141/165** | 11,8 s / **13,9 s** | **$0,107** |
| **Router** | **141/165** | 11,7 s / **39,0 s** (3 timeout) | **$0,174** |
| Luôn mid | 136/165 | 5,8 s / 30,2 s | $0,066 |

Chi phí deepseek do gateway ghi nhận thấp hơn hóa đơn thật 4–5 lần, nên dashboard làm router trông rẻ hơn small trong khi thực tế đắt hơn. Trên bộ gap 30 câu, router chỉ hơn small 1 câu (24 so với 23) nhưng p95 chậm khoảng 20 lần (101 s so với 4,8 s).

**Nâng tier quá tay và theo dạng câu.**

| Bộ | Recall (small sai → nâng) | Nâng thừa (small đúng vẫn nâng) | Precision |
|---|---|---|---|
| 165 câu chính | 14/23 (61%) | 60/139 (43%) | **14/74 (19%)** |
| 30 câu gap | 5/5 | 17/21 (81%) | 5/22 (23%) |

- Khoảng 5 lần nâng thì chỉ 1 lần cần. Router vẫn lọt 9/23 câu small làm sai.
- Medical: nâng 29/30 câu, router đúng 21/30 trong khi small đúng 22/30, chi phí gấp 3 lần.

**Không biết khi nào cần effort high.** Trên `harder_cases`, 58 câu lên effort high nhưng chỉ 7 câu cần (51 câu effort low cũng làm đúng). Mọi câu thi toán được jev chấm reasoning = 3, nên luật R1 đẩy thẳng lên large; tier mid hầu như không được dùng (9/96). Tier large tốn $2,08 so với $0,95 nếu chạy effort low.

**Cấu hình pool sai giá.** Trên `hard_cases`, Router A đắt hơn luôn large vì Terra (mid) tính theo token đắt hơn gpt-6.1-sol (large): 36 câu qua Terra tốn $0,204, chạy trên Sol chỉ $0,110. Router B dùng effort high làm chi phí tier large tăng gấp đôi mà không đúng thêm câu nào.

**Tập test bên ngoài (RouterArena).** Router chỉ hơn chia ngẫu nhiên cùng chi phí 0,8 điểm; AUC tách câu small làm sai là 0,63 (cần ≥ ~0,8); recall large 1%. Với nhãn độ khó gốc: đúng 55%, QANTA_Literature 5%, WMT19 gu-en 0%. Nguyên nhân: model nhỏ sai vì **không biết dữ kiện**, điều mà đọc prompt không đoán được.

**Chọn tier sai trên các bộ gán nhãn khác:** Arena-Hard (toán, code) balanced chỉ 40,3% (chọn đắt hơn mức cần); context bench rag_context 5/7 chọn đắt hơn khi câu trả lời có sẵn trong tài liệu; LMArena (nhãn Llama-3-70B, chỉ tham khảo) đúng 35%.

### 2.4 Unit Tests

Chạy ngày 2026-10-17 trên Windows 11, Python 3.11:

```
python -m pytest tests/ --ignore=tests/test_api/test_openai_proxy_campaign.py
33 failed, 1281 passed, 9 skipped, 46 warnings, 4 errors in 578.47s (0:09:38)
```

| Kết quả | Số test |
|---|---|
| Passed | **1281** (97,2% số test có chạy) |
| Failed | 33 |
| Error | 4 |
| Skipped | 9 |

`test_openai_proxy_campaign.py` bị loại vì import `resource` (module chỉ có trên Unix). File này vẫn luôn bị skip nếu không đặt `OPENAI_AUDIT_PROXY=1` (cần Docker/Nginx).

Nguyên nhân lỗi, nhóm theo loại:

| Nhóm | Ví dụ | Ghi chú |
|---|---|---|
| Chỉ lỗi trên Windows | `os.fchmod` không tồn tại (latency dataset/trainer); đường dẫn `/tmp` (4 error ở `test_merge_openai_live_test.py`); `UnicodeDecodeError` cp1252 khi đọc file không chỉ định `encoding`; `WinError 87` (process supervisor); `test_package_boundaries` | Có thể pass trên Linux/CI, cần chạy lại để xác nhận |
| Thiếu môi trường | `Run npm ci in tests/sdk_typescript first` | TypeScript SDK test |
| Code và test lệch nhau | `ApplicationService` không có `archive`; API pool trả 422 ("Active model not found") thay vì 201; playground trả 403 "Insufficient role"; mock `SimpleNamespace` thiếu `scalars` ở `provider_credentials.py:35`; field Responses SDK chưa được phân loại | Lỗi thật, cần sửa |
| Không ổn định theo thời gian | `assert 16.0 < 15.0` (observed provider); `provider_total_ms must be positive` | Có thể flaky trên Windows (độ phân giải đồng hồ) |

### 2.5 Integration Tests

Phép đo end-to-end là answer bench: chạy cả pipeline public runtime (understander `jev`, decomposer `llm`, router `database`) trên các bộ câu có đáp án chuẩn, so với các cấu hình luôn small / luôn mid / luôn large. Kết quả ở mục 2.1–2.3.

### 2.6 Giới hạn của các phép đo

- Mỗi cấu hình chỉ chạy **1 lượt**. Giữa hai lượt có 9–17/165 câu đổi đúng/sai, nên chênh 1–2 câu (ví dụ 97 so với 98) không kết luận được.
- Kết quả đề khó đo với timeout 300 s mỗi lần gọi. Production hiện là 30 s mỗi lần gọi, hạn chót 120 s, giới hạn 800 token output, nên tier large sẽ timeout ở chính các câu khó.
- Phần đề khó chủ yếu là toán thi. Bộ đồng thuận, jev benchmark và context bench là tập dev hoặc case tự viết, nên con số có phần lạc quan.
- Bộ 165 câu so router proxy3 với flash-lite (pool cũ), chưa đo lại với pool GPT-6 hiện tại không tách task.
- Judge là jev; câu quan trọng ở đề khó đã kiểm tay, đề thường chưa kiểm tay.
- Chi phí thực tế của deepseek là ước tính theo hóa đơn một ngày.

## 3. User Feedback

Chưa thu thập.

| User | Feedback | Rating |
|------|----------|--------|
| [User 1] | [feedback] | [1-5] |
| [User 2] | [feedback] | [1-5] |

## 4. Demo Results

- Ngày demo: [YYYY-MM-DD]
- Người tham gia: [số người]
- Feedback chung: [tóm tắt]
- Issues phát hiện (từ eval):
  - Router ngang luôn small trên traffic tiếng Việt thường nhưng tốn hơn 1,6–5 lần tùy pool, chậm hơn ở p95.
  - Nâng tier theo dạng câu (y khoa, toán thi) chứ không theo việc model nhỏ có làm sai không; precision nâng tier 19%.
  - Gửi gần hết câu thi lên effort high dù chỉ 7/58 câu cần.
  - Tier mid trong pool GPT đắt hơn tier large.
  - Timeout production quá ngắn cho tier large; retry sau timeout làm lại toàn bộ (apex-010 mất 571 s, có thể bị tính tiền 2 lần).
  - `insufficient_quota` (429 hết credit) bị coi là lỗi tạm thời và retry vô ích.

## 5. Action Items

- [ ] Đặt effort mặc định của tier large là `medium`; chỉ lên effort high khi có tín hiệu riêng (cờ olympiad, tự kiểm tra rồi mới leo tier).
- [ ] Thêm tín hiệu "model nhỏ có làm sai không", ví dụ `small_model_trap` (AUC 0,74, tốt nhất hiện có), thay vì chỉ dựa vào độ khó nhìn thấy.
- [ ] Bảo đảm giá tier mid thấp hơn tier large trong mọi pool.
- [ ] Không retry sau timeout với model suy luận; đặt timeout riêng cho từng tier; không retry lỗi `insufficient_quota`.
- [ ] Áp dụng bộ nhãn `quality_tier` đề xuất cho catalog (78% → 100% đúng tier).
- [ ] Sửa chi phí deepseek trong gateway cho khớp hóa đơn.
- [ ] Chạy lại mỗi cấu hình nhiều lượt; đo lại bộ 165 câu với pool GPT-6 không tách task; đo routing từng subtask.
- [ ] Sửa các test lỗi do code và test lệch nhau (application archive, pool API, playground role, mock `provider_credentials`); chạy lại cả bộ trên Linux để tách lỗi chỉ có trên Windows.
- [ ] Thu thập user feedback và đo test coverage.
