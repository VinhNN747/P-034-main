# Weekly Journal — Team P-034

---

## Week 1: 2026-09-14 - 2026-09-20

### Mục tiêu tuần này
- [x] Kiểm tra cơ chế AI Log, hooks và môi trường lab.
- [x] Xác định hướng routing/decompose của dự án.
- [x] Thiết lập nền tảng làm việc và logging.

### Đã hoàn thành
- Kiểm tra Tool Schema, session logging và AI Log pipeline.
- Thiết lập cơ chế nhận log và xác định hướng routing bằng JEV.
- Xác định các vấn đề ban đầu liên quan đến hook và đồng bộ log.

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Log chưa được ghi/nhận nhất quán giữa các công cụ | Kiểm tra hooks, session và script submit log | Xác định được pipeline logging cần chuẩn hóa |
| Chưa rõ ranh giới giữa decompose và routing | Phân tích pipeline và thử nghiệm JEV | Có hướng phân tách module rõ ràng |

### Bài học
- Cần kiểm tra event log thực tế thay vì chỉ dựa vào tên file.
- Routing và decompose nên là hai module độc lập để dễ kiểm thử.

### Kế hoạch tuần sau
- [x] Hoàn thiện route → execute/fallback.
- [x] Khởi tạo frontend và UI nền tảng.
- [x] Bổ sung evaluation/aggregate cho pipeline.

---

## Week 2: 2026-09-21 - 2026-09-27

### Mục tiêu tuần này
- [x] Hoàn thiện routing, execute/fallback, aggregate và evaluation.
- [x] Xây lại pipeline và chia spec cho làm việc song song.
- [x] Triển khai frontend, authentication/RBAC và gateway backend.

### Đã hoàn thành
- Thêm JEV-based routing, retry/fallback, token/cost trace và evaluation harness.
- Tạo pipeline mới, cập nhật spec và triển khai Track A/Track B.
- Khởi tạo Next.js frontend, Supabase Auth, JWT/RBAC và gateway runtime.
- Triển khai embedding, evidence, execution runtime, provider adapters và observability.

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Pipeline cũ khó mở rộng và khó làm song song | Tách pipeline cũ, viết lại spec và chia Track A/Track B | Có cấu trúc mới rõ ràng hơn |
| Routing failure và trạng thái subtask không nhất quán | Chuẩn hóa retryable status, fallback và trace | Route → execute có fallback rõ ràng |
| Auth/RBAC và gateway cần tích hợp đồng thời | Tách migration, permission matrix, runtime và UI | Hình thành nền tảng end-to-end |

### Bài học
- Spec cần mô tả rõ interface giữa các track trước khi code song song.
- Observability và trace nên được xây cùng pipeline, không bổ sung sau.

### Kế hoạch tuần sau
- [x] Tích hợp FE/BE và automatic aggregation.
- [x] Cải thiện evidence, calibration và model selection.
- [x] Ổn định database, billing và gateway contracts.

---

## Week 3: 2026-09-28 - 2026-10-04

### Mục tiêu tuần này
- [x] Tích hợp frontend/backend và hoàn thiện gateway flow.
- [x] Cải thiện routing theo evidence, latency, cost và difficulty.
- [x] Chuẩn hóa database, model pool, SDK và deployment.

### Đã hoàn thành
- Tích hợp FE/BE, automatic aggregation, chatbot/demo UI và bản dịch.
- Bổ sung LLMRouterBench/BGE-M3 evidence, calibration, latency-first routing và model pool.
- Refactor database/service, tối ưu connection pool, thêm observability pagination.
- Xây typed Python SDK, gateway runtime keys, GCR/LiteLLM engine và Docker/Redis setup.

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Router thiếu dữ liệu và khó chọn model phù hợp | Dùng evidence, difficulty, latency/cost objective và model pool | Router có thêm tín hiệu và fallback |
| Gateway/aggregation không ổn định với JSON và billing | Hardening strict JSON, debit quantization và aggregation contract | Runtime ổn định hơn |
| Database connection dễ dư thừa | Tối ưu engine lifecycle, pool và authorization cache | Cải thiện concurrency và resource usage |

### Bài học
- Chất lượng router phụ thuộc cả dữ liệu, objective và chiến lược fallback.
- Model pool cần gắn với quyền, provider capability và observability.

### Kế hoạch tuần sau
- [x] Hoàn thiện UI/UX mới và OpenAI compatibility.
- [x] Benchmark full pipeline và cải thiện decompose.
- [x] Chuẩn bị offline pipeline, provider credentials và admin UI.

---

## Week 4: 2026-10-05 - 2026-10-11

### Mục tiêu tuần này
- [x] Overhaul UI/UX và tích hợp các phần hệ thống vào UI mới.
- [x] Hoàn thiện OpenAI-compatible API, SDK và provider/model admin.
- [x] Benchmark routing/decompose và xử lý các vấn đề vận hành.

### Đã hoàn thành
- Cải tổ UI/UX, sorting, routing map integration, application/settings và observability.
- Thêm responses/models/embeddings API, SDK compatibility, provider credentials và model admin.
- Xây comparison worker, support chatbot, Highcharts analytics và request metrics.
- Cải thiện tier routing, context signals, full-pipeline benchmark và live routing.

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| UI có nhiều màn hình và trạng thái sorting phức tạp | Chuẩn hóa component, state và sorting interactions | UI đồng nhất và dễ sử dụng hơn |
| Cần tương thích với OpenAI SDK và provider thật | Bổ sung compatibility endpoints, credential resolver và smoke tests | Có profile tương thích rõ ràng |
| Connection pool/triển khai gặp lỗi thực tế | Kiểm tra connection lifecycle, Docker và Render flow | Giảm rủi ro khi deploy |

### Bài học
- UI observability cần phản ánh đúng request lifecycle và routing trace.
- Smoke test với provider thật cần được tách khỏi test offline nhưng chạy trong cùng workflow kiểm chứng.

### Kế hoạch tuần sau
- [x] Hoàn thiện offline pipeline và BYO endpoints.
- [x] Bổ sung difficulty classifier và budget alerts.
- [x] Chốt deploy version 1 và dọn dẹp tài liệu.

---

## Week 5: 2026-10-12 - 2026-10-17

### Mục tiêu tuần này
- [x] Hoàn thiện offline pipeline, classifier và endpoint management.
- [x] Hoàn thiện security, budget alerts, chatbot và deployment.
- [x] Refactor, dọn dẹp và chốt trạng thái phát hành.

### Đã hoàn thành
- Triển khai offline pipelines, customer endpoints, classifier và evaluation datasets.
- Thêm local task-type/difficulty classifier, budget management, alert notifications và Resend config.
- Hoàn thiện password recovery, account security, chatbot FAQ animation và ChatWelcome.
- Hoàn thiện deploy version 1, refactor code structure và dọn generated markdown.

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Router cần hoạt động với dữ liệu/provider ngoài môi trường chuẩn | Bổ sung offline pipeline, BYO endpoints và classifier shadow | Có đường chạy linh hoạt hơn |
| Vận hành cần cảnh báo chi phí và bảo mật tài khoản | Thêm budget alerts, Resend config, recovery và security flow | Tăng khả năng vận hành an toàn |
| Nhiều thay đổi UI/backend cần hợp nhất trước deploy | Refactor, kiểm tra deploy và dọn generated files | Chốt được deploy version 1 |

### Bài học
- Các quyết định routing cần được kiểm chứng bằng benchmark và request thực tế.
- Worklog nên dùng ngày event trong log và hash commit để truy vết rõ ràng.
- Tài liệu và observability là một phần của sản phẩm, không chỉ là phần bổ sung.

