# Frontend Taskway

Giao diện Next.js nằm trong `src/frontend/`. Từ thư mục gốc repo, cài dependency và chạy:

```bash
npm ci --prefix src/frontend
npm run dev --prefix src/frontend
```

Mở `http://localhost:3000`. Tạo `src/frontend/.env.local` với cấu hình Supabase và URL của hai backend theo [hướng dẫn chạy local](../../README.md#chạy-local). Frontend vẫn đọc `.env` ở gốc repo khi cần các biến dùng chung; các biến riêng của frontend nên đặt trong `.env.local`.

Mã giao diện và API route của Next.js nằm trong `src/frontend/src/`. Để kiểm tra bản production:

```bash
npm run build --prefix src/frontend
```
