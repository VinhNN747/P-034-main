# Taskway — AI Gateway

Taskway là ứng dụng quản lý và vận hành AI Gateway. Người dùng tạo ứng dụng và API key, cấu hình danh mục model, provider và chính sách định tuyến trên giao diện web. AI backend nhận yêu cầu, chọn model, xử lý dự phòng và ghi nhận lượt sử dụng.

Repo này được phát triển từ AI20K Agent Template, nhưng mã nguồn hiện tại là một ứng dụng gồm hai backend và một frontend Next.js. Tài liệu khóa học gốc vẫn nằm trong [docs/guide/](docs/guide/).

## Thành phần chính

| Thành phần | Vai trò | Cổng khi chạy local |
|---|---|---:|
| `src/backend/` | API quản trị: ứng dụng, API key, provider, model, cấu hình Gateway, billing và báo cáo | 8000 |
| `src/backend_ai/` | API thực thi AI: phân tích và xử lý yêu cầu Gateway, định tuyến model, gọi provider, các endpoint tương thích OpenAI | 8001 |
| `src/shared/` | Cấu hình, mô hình dữ liệu, xác thực và nghiệp vụ dùng chung cho hai backend | — |
| `src/frontend/` | Giao diện Taskway dùng Next.js | 3000 |

Hai backend dùng chung cơ sở dữ liệu. Management backend gọi AI backend qua HTTP cho các thao tác cần thực thi AI. Xem [kiến trúc](ARCHITECTURE.md) và [sơ đồ hệ thống](docs/architecture_diagram.md) để biết chi tiết luồng xử lý.

## Chạy local

### Yêu cầu

- Python 3.11 trở lên, Node.js và npm.
- PostgreSQL/Supabase với schema phù hợp cho ứng dụng, bao gồm các bảng phân quyền nền tảng `public.roles`, `public.permissions`, `public.role_permissions`.
- Docker Compose nếu muốn chạy hai backend trong container.

### 1. Cài dependencies

Tại thư mục gốc của repo:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/requirements.txt
npm ci --prefix src/frontend
```

`src/requirements.txt` cài dependencies cho cả hai backend và công cụ phát triển. Frontend đã khai báo `highcharts` trong `src/frontend/package.json`, nên `npm ci` là đủ.

### 2. Cấu hình backend và cơ sở dữ liệu

```bash
cp src/.env.example .env
```

Điền `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` và `AI_SERVICE_TOKEN` trong `.env`. Hai backend phải dùng cùng `DATABASE_URL` và `AI_SERVICE_TOKEN`. Cấu hình thêm secret key phía server của Supabase nếu dùng chức năng cần Supabase Admin API.

#### Provider credentials

`src/.env.example` chọn `GATEWAY_EXECUTION_MODE=online` (tương ứng chế độ `live` trong mã nguồn). Chế độ này yêu cầu `CREDENTIAL_ENCRYPTION_KEY`; tạo khóa Fernet một lần và cấp cùng giá trị cho cả hai backend:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Sao lưu khóa ngoài Git: nếu mất hoặc đổi khóa, các credentials đã lưu trong cơ sở dữ liệu sẽ không giải mã được. Provider key được nhập trong giao diện **Providers**, không đặt trong `.env`. Nếu chỉ muốn chạy pipeline không gọi provider bên ngoài, đặt `GATEWAY_EXECUTION_MODE=offline`; chế độ này không yêu cầu khóa mã hóa.

Các migration nằm trong `src/alembic/versions/`. Với database đã có các bảng phân quyền nền tảng nêu trên, áp dụng schema của ứng dụng bằng:

```bash
alembic -c src/alembic.ini upgrade head
```

### 3. Cấu hình frontend

Tạo `src/frontend/.env.local` với giá trị thực của dự án Supabase:

```dotenv
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=your-publishable-key
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_AI_API_URL=http://localhost:8001
BACKEND_API_URL=http://127.0.0.1:8000
```

Frontend đọc `src/frontend/.env.local` và nạp thêm `.env` ở gốc repo cho biến dùng chung; Python backend cũng đọc `.env` ở gốc. Cả hai file cấu hình local đều được Git bỏ qua.

### 4. Khởi chạy

Mở ba terminal tại thư mục gốc repo; bật `.venv` trong hai terminal chạy Python:

```bash
uvicorn src.backend.main:app --reload --port 8000
```

```bash
uvicorn src.backend_ai.main:app --reload --port 8001
```

```bash
npm run dev --prefix src/frontend
```

Truy cập giao diện tại `http://localhost:3000`. Hai backend có health endpoint tại `http://localhost:8000/health` và `http://localhost:8001/health`.

Để gửi yêu cầu qua Gateway, cần có ứng dụng, model khả dụng và cấu hình định tuyến trong cơ sở dữ liệu. Có thể tạo chúng trên giao diện quản trị; `src/scripts/seed_backend.py` cung cấp dữ liệu khởi tạo mẫu và nhận `--owner-id` để gán ứng dụng cho đúng người dùng.

Để chạy hai backend bằng Docker Compose sau khi cấu hình `.env`:

```bash
docker compose --env-file .env -f src/docker-compose.yml up --build
```

Nếu Docker trên máy chưa cài plugin Buildx, có thể dùng builder truyền thống cho hai backend:

```bash
DOCKER_BUILDKIT=0 docker compose --env-file .env -f src/docker-compose.yml up --build
```

`src/docker-compose.yml` chạy hai backend theo mặc định; frontend vẫn chạy riêng bằng npm. Compose có thêm profile `proxy` cho Nginx và `standalone-comparison` cho comparison worker.

## API

| Địa chỉ local | Nội dung |
|---|---|
| `http://localhost:8000/api/v1` | API quản trị cho ứng dụng, key, provider, model, cấu hình, billing và quan sát |
| `http://localhost:8001/api/v1` | API chat mẫu, phân tích yêu cầu, thực thi Gateway và runtime |
| `http://localhost:8001/v1` | Các endpoint tương thích OpenAI, gồm models, chat completions, responses, embeddings và files |

Khi dùng OpenAI SDK, đặt `base_url=http://localhost:8001/v1` và dùng **application API key** do Taskway cấp. Model có thể là alias như `auto` hoặc model cụ thể khả dụng với ứng dụng. Các endpoint `/api/v1/gateway/analyze` và `/api/v1/gateway/requests` phục vụ các mục đích khác nhau; xem [kiến trúc](ARCHITECTURE.md) trước khi tích hợp.

## Cấu trúc repo

```text
src/                    Mã ứng dụng và cấu hình chạy
  backend/              Backend quản trị
  backend_ai/           Backend AI
  shared/               Mã dùng chung
  frontend/             Frontend Next.js
  classifier_service/   Dịch vụ phân loại tác vụ
  alembic/              Migration cơ sở dữ liệu
  scripts/              Công cụ hỗ trợ phát triển, vận hành
  deploy/               Cấu hình triển khai
  Dockerfile            Image cho hai backend
  .dockerignore         Giới hạn build context của hai backend
  docker-compose.yml    Docker Compose cho hai backend
  .env.example          Mẫu cấu hình local
tests/                  Kiểm thử Python và hợp đồng API
docs/guide/             Technical Guidebook của AI20K
docs/architecture_diagram.md  Sơ đồ kiến trúc hệ thống
eval/                   Tài liệu và kết quả đánh giá
presentation/           Tài liệu trình bày, video demo
```

Xem [quy tắc phân chia package](src/README.md). CI trong `.github/workflows/ci.yml` chạy Ruff, pytest và các kiểm tra tích hợp cho backend.

## Tài liệu khác

- [Kiến trúc ứng dụng](ARCHITECTURE.md)
- [Sơ đồ kiến trúc hệ thống](docs/architecture_diagram.md)
- [Technical Guidebook AI20K](docs/guide/)

## Giấy phép

[MIT](LICENSE).
