# Sơ đồ kiến trúc hệ thống

Sơ đồ tổng quan (System Architecture Diagram) dưới đây thể hiện các ứng dụng đang chạy, phần mã dùng chung, nơi lưu dữ liệu và các kết nối chính. Bên trong backend AI, sơ đồ phân biệt bước phân tích yêu cầu với hai nhánh thực thi chính.

```mermaid
flowchart LR
    subgraph CLIENTS["Phía sử dụng"]
        USER([Người dùng])
        SDK["Ứng dụng dùng OpenAI SDK"]
    end

    subgraph PRODUCT["Các thành phần trong repo"]
        FE["Giao diện web<br/>Next.js · src/frontend/"]
        MGMT["Backend quản trị<br/>FastAPI · src/backend/ · cổng 8000"]
        subgraph AI_BACKEND["Backend AI · FastAPI · src/backend_ai/ · cổng 8001"]
            AI_API["API chat, Gateway, runtime và /v1"]
            PIPE["RequestPipeline<br/>kiểm tra · hiểu · tách tác vụ nếu cần"]
            ANALYZE["Trả kết quả phân tích"]
            ORCH["GatewayOrchestrator + SubtaskExecutor<br/>định tuyến · chạy tác vụ · tổng hợp"]
            MESSAGE["Nhánh OpenAI message<br/>thực thi một tác vụ"]
            RECORD["Lưu kết quả, mức sử dụng và khoản tính phí"]
        end
        SHARED["Thư viện dùng chung<br/>src/shared/"]
    end

    subgraph EXTERNAL["Dữ liệu và dịch vụ bên ngoài"]
        DB[(PostgreSQL dùng chung)]
        PROVIDERS["Nhà cung cấp mô hình<br/>hoặc endpoint tự quản"]
    end

    USER -->|Sử dụng| FE
    FE -->|API quản trị, báo cáo, tính phí| MGMT
    FE -->|Trò chuyện và thực thi AI| AI_API
    SDK -->|API tương thích OpenAI: /v1| AI_API
    MGMT -->|HTTP nội bộ có xác thực| AI_API
    AI_API -->|Yêu cầu chat hoặc Gateway| PIPE
    PIPE -->|/gateway/analyze| ANALYZE
    PIPE -->|Yêu cầu thực thi thông thường| ORCH
    PIPE -->|Một số yêu cầu /v1 chat| MESSAGE
    PIPE -->|LLM, JEV hoặc OCR khi được cấu hình| PROVIDERS
    ORCH --> RECORD
    MESSAGE --> RECORD
    ORCH -->|Gọi mô hình| PROVIDERS
    MESSAGE -->|Gọi mô hình| PROVIDERS
    AI_API -->|Hỗ trợ và kiểm tra mô hình nội bộ| PROVIDERS
    MGMT -.->|Dùng thư viện| SHARED
    AI_API -.->|Dùng thư viện| SHARED
    MGMT -->|Đọc/ghi dữ liệu quản trị| DB
    AI_API -->|Đọc cấu hình và tập mô hình| DB
    RECORD -->|Ghi biên nhận và chi phí| DB
```

Nét liền thể hiện luồng gọi API hoặc đọc/ghi dữ liệu; nét đứt thể hiện phụ thuộc vào thư viện Python. `src/shared/` được nạp trong cả hai tiến trình backend, không chạy như một dịch vụ HTTP riêng. Các mũi tên từ backend tới PostgreSQL biểu thị việc truy cập thông qua thư viện dùng chung.

Frontend chọn backend theo phương thức và đường dẫn yêu cầu. Backend quản trị gọi các endpoint nội bộ của backend AI để tổng hợp câu trả lời hỗ trợ và kiểm tra mô hình; các endpoint này có thể gọi nhà cung cấp mà không đi qua `RequestPipeline`. Trong luồng Gateway, `RequestPipeline` kiểm tra yêu cầu, giải quyết ràng buộc và tạo đồ thị tác vụ. Bước này có thể gọi LLM, JEV hoặc OCR tùy chiến lược được cấu hình. Endpoint `/api/v1/gateway/analyze` trả kết quả phân tích tại đây, trước bước thực thi các tác vụ để tạo câu trả lời. Yêu cầu thực thi thông thường đi tiếp qua `GatewayOrchestrator`, nơi các tác vụ được định tuyến, gọi mô hình và tổng hợp theo quan hệ phụ thuộc.

Một số yêu cầu `/v1/chat/completions` cần giữ nguyên cấu trúc message đi qua nhánh thực thi một tác vụ riêng. Kết quả thực thi được ghi cùng mức sử dụng và chi phí vào PostgreSQL. API `/v1/embeddings` có luồng xử lý riêng. Xem [tài liệu kiến trúc](../ARCHITECTURE.md) để đọc luồng AI Gateway chi tiết và quy tắc phân chia package.
