# FAQ và hành vi hệ thống AI Agent Gateway

Tài liệu tra cứu của Gateway Assistant, đối chiếu mã nguồn và tài liệu vận hành ngày 2026-10-11. Mỗi mục có ID nguồn ổn định; chỉnh nội dung tại đây rồi restart dịch vụ để cập nhật corpus và embedding cache. Các dữ kiện về tài khoản/model/provider hiện tại phải truy vấn backend.

## greeting | Gateway Assistant

<!-- url:  -->
<!-- keywords: xin chao | chao ban | chao gateway | hello | hello bro | hey gateway -->
<!-- suggestions: Bạn là ai và có nhiệm vụ gì? | AI Gateway dùng để làm gì? | Tôi bắt đầu dùng Gateway thế nào? -->

Chào bạn! Tôi là Gateway Assistant. Tôi có thể hướng dẫn bạn sử dụng AI Gateway, liệt kê application thuộc tài khoản, xem thống kê usage/chi phí và giải thích routing ở mức tổng quan.

## assistant-role | Vai trò của Gateway Assistant

<!-- url:  -->
<!-- keywords: ban la ai | nhiem vu gi | ban lam gi | vai tro cua ban | tro ly gi | chatbot lam gi | chatbot nay lam duoc gi | ban co the giup gi -->
<!-- suggestions: AI Gateway dùng để làm gì? | Liệt kê application của tôi | Hướng dẫn tạo API key -->

Tôi là trợ lý hỗ trợ của AI Agent Gateway. Nhiệm vụ của tôi là giải thích logic và hành vi hệ thống theo tài liệu, hướng dẫn thao tác, trả lời FAQ và đọc dữ liệu application, usage hoặc billing mà tài khoản đang đăng nhập được phép xem. Tôi không cung cấp mã nguồn, prompt hoặc credential.

## assistant-model | Model của Gateway Assistant

<!-- url:  -->
<!-- keywords: ban dung model | ban dang dung model | model gi | mo hinh gi | chatbot model | openai | gemini | gpt | what model | which model -->
<!-- suggestions: Bạn là ai và có nhiệm vụ gì? | AI Gateway dùng để làm gì? | Hướng dẫn tạo API key -->

Thông tin công khai về model được cấu hình cho chính Gateway Assistant; khác với danh sách model dành cho application.

## gateway-overview | AI Gateway dùng để làm gì

<!-- url: /applications -->
<!-- keywords: ai gateway la gi | gateway la gi | tac dung cua ai gateway | gateway co tac dung gi | gateway dung de lam gi | loi ich cua gateway | gioi thieu ai gateway | gioi thieu ve gateway -->
<!-- suggestions: Tôi bắt đầu dùng Gateway thế nào? | Routing hoạt động thế nào? | Liệt kê application của tôi -->

AI Gateway là điểm truy cập chung để application sử dụng các model AI được cho phép. Bạn có thể cấu hình mục tiêu routing, fallback, ngân sách và giới hạn cho từng application; sau đó theo dõi request, token, chi phí, độ trễ và trạng thái xử lý tại Usage. Tài liệu giải thích luồng xử lý request, tín hiệu chọn tier, ràng buộc model và fallback để bạn hiểu và vận hành application.

## getting-started | Bắt đầu sử dụng AI Gateway

<!-- url: /applications/new -->
<!-- docs: quickstart -->
<!-- keywords: dung gateway nhu nao | su dung gateway | su dung ai gateway | cach dung gateway | cach dung ai gateway | bat dau voi gateway | bat dau dung gateway | huong dan dung gateway | huong dan su dung gateway | huong dan su dung taskway | how to use gateway | how do i use taskway -->
<!-- suggestions: Hướng dẫn tạo application | Xác thực và cấu hình SDK thế nào? | Cách đọc đầu ra của API? -->

Để bắt đầu: 1) mở Applications và tạo application; 2) chọn model khả dụng, mục tiêu routing, fallback và giới hạn ngân sách; 3) tạo API key cho application; 4) chạy thử trong Playground; 5) theo dõi request, token, chi phí và độ trễ tại Usage. Bạn nên bắt đầu bằng một application development rồi mới dùng key trong dịch vụ của mình.

## create-application | Tạo application

<!-- url: /applications/new -->
<!-- keywords: tao application | tao ung dung | new application | create application -->
<!-- suggestions:  -->

Bạn tạo application tại Applications → New application. Điền tên, slug, môi trường và cấu hình gateway cần thiết. Sau khi tạo, mở trang application để cấu hình, tạo API key hoặc chạy Playground.

## application-api-key | API key của application

<!-- url: /applications -->
<!-- docs: api-key -->
<!-- keywords: api key | api-key | tao key | create key | application key -->
<!-- suggestions:  -->

API key thuộc về từng application. Mở application cần dùng, vào phần API keys rồi tạo key. Chỉ dùng key ở môi trường server của application; không gửi key qua chat hoặc lưu vào frontend.

## playground | Playground

<!-- url: /applications -->
<!-- keywords: playground | test routing | thu routing | chay thu -->
<!-- suggestions:  -->

Playground chạy request cho một application mà bạn sở hữu. Request được xử lý bởi Gateway và được ghi vào Usage/quan sát giống một request runtime; vì vậy hãy chọn application và giới hạn phù hợp trước khi chạy.

## usage-and-billing | Usage và Billing

<!-- url: /usage -->
<!-- keywords: usage | token | chi phi | cost | billing | credit | so du | balance -->
<!-- suggestions:  -->

Usage hiển thị request, token, chi phí và độ trễ theo dữ liệu Gateway đã ghi. Billing hiển thị credit và giao dịch của tài khoản. Bạn cũng có thể hỏi tôi số liệu hiện tại của chính mình.

## available-models | Available models

<!-- url: /available-models -->
<!-- keywords: available model | model nao | danh sach model | models -->
<!-- suggestions:  -->

Trang Available models cho biết các model hiện có cho workspace. Trạng thái và capability của model được quản lý từ catalog; model chỉ nên được dùng khi cấu hình application cho phép.

## authentication-errors | Lỗi đăng nhập và quyền

<!-- url:  -->
<!-- keywords: 401 | 403 | khong co quyen | permission | access denied | dang nhap -->
<!-- suggestions:  -->

401 thường nghĩa là phiên đăng nhập không còn hợp lệ; hãy đăng nhập lại. 403 nghĩa là tài khoản đã đăng nhập nhưng chưa có quyền cho thao tác hoặc tài nguyên đó. Quyền được xác minh tại backend, không chỉ ở menu.

## routing-overview | Routing và đường đi của request

<!-- url: /applications -->
<!-- keywords: routing | định tuyến | logic hệ thống -->
<!-- suggestions:  -->

Ở mức tổng quan, Gateway xác thực application, đọc cấu hình và ràng buộc, phân tích mục tiêu và ngữ cảnh, quyết định có tách tác vụ, chọn model đủ điều kiện, thực thi, tổng hợp kết quả rồi ghi request, usage và billing. Các bước hiểu yêu cầu và chọn model có thể dùng chiến lược khác nhau theo cấu hình. Bạn xem model đã chạy, thời gian, lỗi và chi phí ở Requests/Usage. Tài liệu mô tả hành vi hệ thống; không cung cấp mã nguồn, prompt hay credential nội bộ.

## application-settings | Cấu hình, trạng thái và quyền sở hữu application

<!-- url: /applications -->
<!-- keywords: application settings | cấu hình | inactive -->
<!-- suggestions:  -->

Application có tên, slug, môi trường và trạng thái. Settings cấu hình mục tiêu routing, model pool tùy chọn, routing policy tùy chọn, fallback, ngân sách, giới hạn output và các strategy được cho phép. Trạng thái inactive có thể khiến request runtime bị từ chối. Chỉ chủ sở hữu và quyền được backend xác minh mới truy cập dữ liệu riêng của application. Thay đổi cấu hình tạo phiên bản mới; request dùng snapshot cấu hình khi chạy. Chatbot hướng dẫn thao tác và đọc dữ liệu; không tự sửa settings hoặc thực hiện thao tác quản trị.

## model-pools | Model pool và catalog khác nhau thế nào

<!-- url: /model-pools -->
<!-- keywords: pool | catalog | model đủ điều kiện -->
<!-- suggestions:  -->

Catalog chứa model/provider được quản trị viên quản lý. Model pool là tập model riêng của user, không rỗng; thứ tự thành viên không phải thứ tự ưu tiên routing. Application có thể chọn pool hoặc để trống để xét catalog theo cấu hình. Pool giới hạn cả model chính lẫn model fallback; khi application chọn pool, model ngoài pool không trở thành ứng viên chỉ vì các thành viên bị lỗi. Một model trong pool vẫn cần model và provider active, capability phù hợp và đáp ứng ràng buộc của request. Pool không tự cấp quyền dùng model bị chặn. User quản lý pool thuộc tài khoản mình; quyền catalog của admin không đồng nghĩa quyền pool của user.

## routing-objectives | Ưu tiên chi phí, chất lượng và độ trễ

<!-- url: /applications -->
<!-- keywords: cheap | quality | fast | mục tiêu routing -->
<!-- suggestions:  -->

Routing objective thể hiện ưu tiên giữa chi phí, chất lượng và độ trễ trong tập model đủ điều kiện. Qua API tương thích OpenAI, auto dùng mục tiêu của application; auto:cheap ưu tiên chi phí, auto:quality ưu tiên chất lượng, auto:fast ưu tiên độ trễ và tắt tách tác vụ. Ghim tên model vẫn phải thỏa pool, trạng thái, capability và ràng buộc của application. Ưu tiên chất lượng không bảo đảm luôn dùng model đắt nhất; ưu tiên chi phí không cho phép vượt guardrail.

## constraints-and-policy | Ràng buộc request và routing policy

<!-- url: /applications -->
<!-- keywords: constraints | guardrail | policy | budget ceiling -->
<!-- suggestions:  -->

Ràng buộc được kết hợp theo thứ tự platform, application, request và nội dung yêu cầu. Model phải đáp ứng ngân sách, giới hạn thời gian, capability, danh sách model/provider được phép hoặc bị chặn và quy định về provider ngoài. Provider bên ngoài là provider không thuộc loại local/offline, không đồng nghĩa model nằm ngoài pool. Cho phép provider ngoài không mở rộng pool; model của provider đó vẫn phải thuộc pool khi application chọn pool. Ngược lại, fallback sang model local/offline không yêu cầu bật quyền dùng provider ngoài. Routing policy có thể bổ sung scoring và guardrail độc lập với model pool. Khi ràng buộc mâu thuẫn hoặc không còn model đủ điều kiện, Gateway báo lỗi thay vì tự mở rộng phạm vi model. Có thể kiểm tra Settings và chi tiết request để tìm ràng buộc đang loại model.

## fallback-and-health | Fallback, lỗi provider và circuit breaker

<!-- url: /applications -->
<!-- keywords: fallback | retry | provider lỗi | circuit breaker -->
<!-- suggestions:  -->

Fallback là thử phương án dự phòng khi model/provider thực thi gặp lỗi và cấu hình cho phép. Phương án dự phòng vẫn phải thuộc tập model đủ điều kiện và đáp ứng ràng buộc hiện tại; không tự bỏ qua pool, ngân sách hoặc quyền. Nếu application chọn pool, fallback không được chọn model ngoài pool, kể cả khi provider ngoài được cho phép. Nếu toàn bộ model đủ điều kiện trong pool đều lỗi hoặc bị loại, request có thể failed; hệ thống không tự lấy model từ catalog bên ngoài pool để cứu request. Circuit breaker tạm ngăn gọi liên tục một provider đang lỗi. Request trace ghi model đã thử, lỗi và kết quả fallback. Bật fallback không đảm bảo request luôn thành công; nếu không còn phương án phù hợp, request có thể failed.

## routing-signals-and-tiers | Cách phân tích yêu cầu và chọn tier model

<!-- url: /applications -->
<!-- keywords: tier | reasoning | jev | điểm độ khó | luật chọn model -->
<!-- suggestions: Routing objective ảnh hưởng model như thế nào? | Fallback có được chọn ngoài pool không? -->

Bộ hiểu yêu cầu tạo tín hiệu về độ sâu suy luận, chuyên môn, mơ hồ, rủi ro và độ phức tạp đầu ra. Strategy rule, LLM hoặc jev tùy cấu hình; điểm không trực tiếp cấp quyền chạy model. Chính sách tier xét các tín hiệu để chọn small_fast, mid hoặc large_reasoning. Suy luận rất sâu, bài chứng minh/thi đấu hoặc rủi ro cao trong tình huống cụ thể có thể cần large_reasoning. Yêu cầu trung bình, kiến thức hiếm hoặc dạng bài model nhỏ dễ sai có thể cần mid; yêu cầu nhẹ phù hợp small_fast. Độ tin cậy thấp có thể nâng tier; quality_first có thể nâng, cost_first/latency_first có thể hạ khi các điều kiện an toàn cho phép. Ảnh và file không đọc được có thể đặt sàn mid. Câu hỏi cần dữ liệu thời gian thực không được coi là đã có dữ liệu chỉ vì dùng model mạnh hơn. Model thực tế còn phải đáp ứng pool, capability, trạng thái và ràng buộc; trace ghi lý do chọn và điều chỉnh tier.

## decomposition-and-aggregation | Tách tác vụ và tổng hợp kết quả

<!-- url: /applications -->
<!-- keywords: decomposition | subtask | aggregator | tách nhiệm vụ -->
<!-- suggestions:  -->

Một request có nhiều mục tiêu có thể được chia thành các subtask có quan hệ phụ thuộc. Mỗi subtask có thể được định tuyến và thực thi riêng; aggregator ghép kết quả thành câu trả lời cuối. Request đơn giản có thể chạy trực tiếp. Cấu hình và ràng buộc quyết định có được tách hay không; auto:fast tắt tách để ưu tiên tốc độ. Kết quả tổng hợp phụ thuộc dữ liệu subtask thực tế; trạng thái partial có thể cho biết chỉ một phần yêu cầu hoàn thành khi cấu hình cho phép.

## requests-observability | Requests, trace và cách kiểm tra request lỗi

<!-- url: /requests -->
<!-- keywords: request lỗi | failed | trace | trạng thái -->
<!-- suggestions:  -->

Requests cho biết request đã chạy, application, trạng thái và độ trễ theo dữ liệu lưu. Chi tiết request/trace giúp xem các bước xử lý, model/provider đã thử và mã lỗi. Khi request lỗi, kiểm tra application và key còn active, model đủ điều kiện, giới hạn ngân sách/token/thời gian, rồi xem lỗi provider hoặc fallback. User chỉ xem request thuộc application của mình. Request completed, failed hoặc partial phản ánh kết quả xử lý; danh sách model hay FAQ không phải bằng chứng request đã chạy thành công.

## analytics-charts | Thống kê và biểu đồ chatbot hỗ trợ

<!-- url: /usage -->
<!-- keywords: chart | biểu đồ | thống kê | metric -->
<!-- suggestions:  -->

Chatbot có thể đọc số request, token đầu vào/đầu ra/tổng token, chi phí USD, tỷ lệ hoàn thành và độ trễ theo quyền usage. Hỗ trợ tổng hợp chuỗi thời gian theo ngày/tuần, phân bố theo application/source và chi phí hoặc token theo model, so sánh kỳ trước, liệt kê request lỗi gần nhất và xem từng request. Request count, tỷ lệ hoàn thành và latency theo model chưa được hỗ trợ vì không suy đoán phân bổ request giữa các model. Biểu đồ đường phù hợp thời gian, biểu đồ cột phù hợp nhóm. Chỉ vẽ khi người dùng yêu cầu; dữ liệu rỗng không được tự tạo số liệu.

## analytics-periods | Khoảng thời gian, múi giờ và ngày kết thúc

<!-- url: /usage -->
<!-- keywords: hôm qua | 7 ngày | 30 ngày | timezone -->
<!-- suggestions:  -->

Các kỳ có sẵn gồm hôm nay, hôm qua, tuần này, tháng này, 7 ngày và 30 ngày gần nhất. Khoảng tùy chọn dùng hai ngày YYYY-MM-DD, tối đa 90 ngày, ngày kết thúc không bao gồm trong khoảng; khoảng tương lai hoặc ngày không hợp lệ cần làm rõ. Hôm qua là trọn ngày trước theo múi giờ workspace; hôm nay kết thúc ở thời điểm truy vấn. So sánh dùng kỳ trước tương đương; tuần/tháng so theo cùng tiến độ. Các nhãn, đơn vị, múi giờ và thời điểm đọc đi cùng kết quả để tránh hiểu nhầm.

## billing-accounting | Chi phí usage, credit và giao dịch

<!-- url: /billing -->
<!-- keywords: billing | credit | cost | giao dịch -->
<!-- suggestions:  -->

Usage thống kê token và chi phí Gateway đã ghi; Billing hiển thị balance/credit và giao dịch tài khoản. Số request không tự suy ra chi phí vì model, lượng token và các bước xử lý có thể khác nhau. Khi đối chiếu, cần cùng application, khoảng thời gian và loại dữ liệu; xem usage của request và giao dịch liên quan. Giá hoặc credit hiện tại phải đọc từ backend, không lấy con số ví dụ trong tài liệu. Chatbot không nạp credit hay tạo giao dịch khi người dùng chỉ hỏi thông tin.

## openai-compatible-api | SDK và API tương thích OpenAI

<!-- url: /sdk-guide -->
<!-- docs: sdk-configuration -->
<!-- keywords: sdk | chat completions | responses | embeddings | tích hợp -->
<!-- suggestions: Xác thực API Taskway thế nào? | Cách đọc đầu ra của API? | Streaming dùng thế nào? -->

AI backend có các API tương thích OpenAI và runtime native. Application dùng API key của chính application; phiên đăng nhập web khác với key runtime. Với SDK, trỏ base URL đến Gateway được triển khai, dùng key application và model auto hoặc model được cấu hình cho phép. Endpoint hỗ trợ tùy tính năng triển khai; Models, Chat Completions, Responses, Embeddings và Files có contract/giới hạn riêng. Dùng giao diện Settings/Playground để kiểm tra cấu hình và request thực tế; không đưa provider API key vào frontend hoặc gửi qua chatbot.

## taskway-sdk-usage | Hướng dẫn dùng SDK Taskway

<!-- url: /sdk-guide -->
<!-- docs: sdk-installation | sdk-configuration | sdk-request -->
<!-- keywords: huong dan dung sdk | huong dan su dung sdk | cach dung sdk | sdk dung nhu nao | bat dau sdk | cai sdk | python sdk | node sdk | how to use sdk | sdk tutorial | sdk quickstart -->
<!-- suggestions: Xác thực và cấu hình SDK thế nào? | Cách đọc đầu ra của API? | Streaming dùng thế nào? -->

Để dùng SDK với Taskway: 1) cài OpenAI SDK cho Python bằng pip install openai hoặc Node.js bằng npm install openai; 2) khởi tạo client với api_key và base_url (Python), hoặc apiKey và baseURL (Node.js), trỏ base URL đến dịch vụ AI có /v1 ở cuối; 3) gọi client.chat.completions.create với model auto và messages, gửi header Idempotency-Key cho request theo chính sách mặc định; 4) đọc văn bản ở response.choices[0].message.content. Mỗi request mới dùng idempotency key riêng; khi thử lại cùng request, dùng lại key đó. Điều kiện xác thực là một application API key, được tạo trong mục API keys của application và lưu ở máy chủ. SDK Guide và tài liệu Bắt đầu nhanh có ví dụ Python/Node.js; các nút tài liệu bên dưới dẫn tới cài SDK, cấu hình client và gửi request đầu tiên.

## taskway-api-authentication | Xác thực API và cấu hình SDK Taskway

<!-- url: /sdk-guide -->
<!-- docs: authentication | sdk-configuration -->
<!-- keywords: xac thuc api | xac thuc taskway | authentication | authenticate | bearer | cau hinh sdk | configure the sdk | token dang nhap voi application key -->
<!-- suggestions: Hướng dẫn tạo API key | Hướng dẫn sử dụng Taskway | Cách đọc đầu ra của API? -->

API Taskway xác thực bằng application API key trong header Authorization: Bearer, không dùng token đăng nhập web hoặc key của provider. Tạo key trong Applications → chọn application → API keys. Với Python, đặt api_key và base_url; với Node.js, đặt apiKey và baseURL. Base URL phải là endpoint dịch vụ AI có /v1 ở cuối. SDK tự thêm header xác thực từ application key. Lưu key ở máy chủ hoặc secret store. Tài liệu Xác thực có hướng dẫn tạo key, cấu hình SDK và thu hồi key.

## api-response-output | Đọc đầu ra và kết quả của API Taskway

<!-- url: /sdk-guide -->
<!-- docs: chat-output | responses-output -->
<!-- keywords: dau ra | doc ket qua | ket qua tra ve | api output | read output | response output | choices | message content | output_text -->
<!-- suggestions: Hướng dẫn sử dụng Taskway | Xác thực và cấu hình SDK thế nào? | Streaming dùng thế nào? -->

Với Chat Completions, lấy nội dung assistant từ response.choices[0].message.content. Với Responses API, SDK cung cấp response.output_text để đọc văn bản được tạo. Trường usage cho biết usage của model tạo đầu ra; usage.pipeline đã bao gồm usage trong quá trình thực thi của Gateway, nên không cộng hai phần như hai khoản độc lập. Nếu provider không trả một bộ đếm, giá trị đó là chưa biết, không phải bằng 0. Mục Đọc kết quả và usage trong tài liệu Chat Completions giải thích đầu ra; tài liệu Responses hướng dẫn tạo response và tiếp tục hội thoại.

## runtime-history-and-idempotency | Lịch sử chat runtime, streaming và idempotency

<!-- url: /requests -->
<!-- docs: streaming -->
<!-- keywords: history | messages | streaming | idempotency -->
<!-- suggestions:  -->

Gateway runtime không tự nhớ toàn bộ cuộc chat của application: mỗi request cần mang messages và file/context mà model cần thấy. Chatbot FAQ có phiên và lịch sử riêng trong workspace. Streaming trả các phần kết quả theo luồng; cần xử lý trạng thái lỗi/kết thúc và kiểm tra request cuối. Idempotency-Key giúp tránh thực thi lặp lại khi client gửi lại cùng một yêu cầu; dùng lại key cho nội dung khác có thể gây xung đột. Nó khác với cache kết quả; không nên giả định mọi request lặp đều miễn phí.

## files-and-ocr | File, ảnh, PDF và OCR

<!-- url: /applications -->
<!-- keywords: file | pdf | ảnh | ocr | upload -->
<!-- suggestions:  -->

File upload thuộc đúng application, có thời hạn lưu và không thể dùng file_id của application khác. Với request hỗ trợ file, ảnh/PDF hoặc text được phân tích theo loại và cấu hình. PDF có text layer có thể đọc trực tiếp; ảnh hoặc PDF scan có thể cần OCR được bật và provider phù hợp. Ràng buộc không cho provider ngoài hoặc lỗi OCR có thể khiến nội dung file không được đọc đầy đủ; trace thể hiện trạng thái. URL ảnh/file bên ngoài không được mặc định tải về. Không suy đoán nội dung file không truy cập được.

## provider-administration | Provider connection và thao tác admin

<!-- url: /providers -->
<!-- keywords: provider | actions | probe | admin -->
<!-- suggestions:  -->

Admin quản lý provider, model catalog, connection và credential theo quyền. Các action edit/probe trên Models là thao tác quản trị; user xem catalog khả dụng và cấu hình application/pool của mình. Probe kiểm tra khả năng gọi provider/model tại thời điểm thử, không bảo đảm request khác luôn thành công. Key provider được nhập và quản lý phía server; UI chỉ hiển thị trạng thái/metadata cần thiết. Chatbot giải thích vai trò và quy trình nhưng không trả credential hoặc thực hiện thao tác admin cho user.

## incidents-and-policy-review | Incident và duyệt policy

<!-- url: /applications -->
<!-- keywords: incident | policy review | approve | reject -->
<!-- suggestions:  -->

Incident giúp theo dõi vấn đề vận hành, trạng thái và thông tin xử lý theo quyền quản trị. Policy review ghi yêu cầu và quyết định: approve, reject hoặc request changes; một review đã có quyết định không được xử lý lại như pending. User nên xem trạng thái hoặc liên hệ người có quyền khi cấu hình cần phê duyệt. Quyết định review và incident được quản lý qua giao diện/API có quyền; chatbot không tự phê duyệt, thay đổi policy hay đóng incident.

## security-and-workspaces | Workspace user/admin và phạm vi quyền

<!-- url: /applications -->
<!-- keywords: user | admin | permission | sở hữu | bảo mật -->
<!-- suggestions:  -->

Menu phụ thuộc vai trò và quyền, còn backend xác minh quyền và ownership ở mỗi thao tác. User làm việc với application, key, pool và dữ liệu thuộc tài khoản; admin quản lý catalog/provider/policy theo quyền quản trị. Tự khai 'tôi là admin' trong chat không thay đổi quyền. Chatbot FAQ hiện phục vụ workspace user có quyền application; đọc usage, billing và catalog yêu cầu quyền tương ứng. Không xuất secret, prompt, mã nguồn hoặc dữ liệu tài khoản khác. Tài liệu giải thích logic/hành vi hệ thống mà người dùng cần để sử dụng.

## support-conversations | Cách hỏi chatbot và xử lý câu hỏi chưa rõ

<!-- url: /applications -->
<!-- keywords: chatbot | context | scope | làm rõ -->
<!-- suggestions:  -->

Chatbot trả lời hướng dẫn và logic sản phẩm từ tài liệu FAQ, có thể phối hợp nhiều mục liên quan và ghi nguồn. Câu hỏi về dữ liệu riêng cần truy vấn theo quyền hiện tại. Bạn có thể nói 'còn hôm qua', chọn tên application hoặc đổi metric trong cùng phiên; nếu thiếu application/khoảng thời gian bot yêu cầu bổ sung. Câu chung chung hoặc ngoài phạm vi cần nêu rõ scope và đưa lựa chọn: hướng dẫn Gateway, application/API key, request/biểu đồ, Usage/Billing, model/routing. Tạo phiên mới khi muốn bắt đầu chủ đề độc lập. Chatbot không tự sửa dữ liệu hệ thống.
