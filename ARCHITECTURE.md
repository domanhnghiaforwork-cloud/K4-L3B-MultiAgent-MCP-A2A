# L3B Architecture Record

Tài liệu này ghi quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.
Giao diện hàm và cách chạy mock nằm trong `team/INTERFACES.md`.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Case và candidate IDs | Xác thực order, customer history | Customer/order theo tool discovery | `Finding` entity |
| Coordinator | Case, gateway, trace | Handoff, cache theo case, kiểm tra refs | Không tự gọi tool nghiệp vụ | `solve_case()` |
| Order/product | Entity finding | Order, item, seller, product | Order/item/seller/product | `Finding` order |
| Shipment | Entity/order findings | Timeline, trách nhiệm giao hàng | Shipment/seller | `Finding` shipment |
| Payment/refund | Entity/order findings | Capture, split payment, refund | Payment/refund | `Finding` payment |
| Policy | Các findings | Kết luận, trách nhiệm, mức hoàn | Policy và evidence liên quan | Output L3B |
| Conflict resolver | Các findings | Phân giải mâu thuẫn nguồn | Tool chỉ khi cần xác minh | `Finding` conflicts |
| Verifier | Output và findings | Kiểm tra độc lập | Tool chỉ khi cần xác minh | Raise khi không hợp lệ |

Các quyền trên là phạm vi nghiệp vụ. `CaseContext.tool_permissions` đã hỗ trợ
enforcement nhưng chưa bật: cần khám phá tên và input schema của tool MCP thật
trước khi khai báo mapping cụ thể. Tool discovery không đồng nghĩa mọi actor được
gọi mọi tool.

## 3. Entity resolution và A2A protocol

Coordinator gọi entity → order → shipment → payment → conflict → policy → verifier
theo thứ tự cố định, không có vòng lặp. Mỗi bước nhận `CaseContext` giữ `case_id`
và `Finding` của bước trước; handoff chỉ chứa facts, refs và warnings nội bộ.
`task_assigned` và `handoff` được ghi khi giao việc. Các quy tắc xếp hạng/reject
candidate và confidence threshold thuộc module entity, hiện chưa triển khai.
Không ghi nội dung suy luận riêng vào trace.

## 4. Evidence và conflict lifecycle

`EvidenceGateway` xác thực MCP response theo public schema. `CaseContext.fetch()`
tự gắn `case_id`, cache truy vấn trong một case, giữ nguyên `evidence_ref` server trả
và ghi `tool_result_consumed` cho mỗi actor dùng kết quả. Coordinator từ chối refs
trong findings/output/claim nếu chúng chưa được lấy qua context của case này.
Module conflict và policy phải quyết định source precedence, unresolved conflict
và claim linkage; các phần đó vẫn là chỗ triển khai của Người 5.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 0 hiện tại | Báo lỗi, không tạo evidence giả | Không có tool event khi chưa nhận kết quả |
| Entity not found/ambiguous | Do module entity quyết định | Policy chọn trạng thái phù hợp | `handoff` các facts thiếu/chưa rõ |
| Source conflict | Do module conflict quyết định | Giữ unresolved nếu chưa đủ căn cứ | `handoff` conflict finding |
| Invalid specialist result | 0 | Dừng case, không xuất output giả | Không có `verification_completed` |

Cache key gồm tên tool và tham số JSON; cache chỉ sống trong `CaseContext` của một
case. `max_calls` có thể giới hạn số call thật, tính cả lần thất bại, nhưng chưa
đặt con số vì private budget không công khai. Cần bổ sung retry hữu hạn sau khi
biết kiểu lỗi MCP thật; bản nền hiện không tự retry và không đoán dữ liệu thiếu.

## 6. Verification invariants

Coordinator hiện kiểm tra kiểu `Finding`, `case_id` đầu ra và ownership của refs
trong findings/output/claims. CLI kiểm tra output theo JSON Schema. Verifier cần
bổ sung các bất biến nghiệp vụ: entity scope, rejected candidates, claim linkage,
timeline, payment/refund totals, source precedence, responsibility/action và
confidence bounds.

## 7. Reproducibility

Python >=3.11; dependency ranges ở `pyproject.toml`. Coordinator hiện chạy tuần tự
trong từng case; CLI chạy tuần tự 100 case. Chưa chọn model hoặc random seed cho
phần nghiệp vụ. Chạy `day09 validate-inputs`, `day09 run`, `day09 validate`, rồi
`day09 package --output dist/submission.zip` sau khi các module được triển khai.
Không ghi API key vào source, trace hoặc output.
