# Giao diện chung để 5 người làm song song

Đây là hợp đồng **nội bộ** của nhóm. JSON nộp bài vẫn phải theo
`contracts/schemas/l3b-output-v2.schema.json`; không đưa `Finding` vào output.

## Phần người 1 đã cố định

- `workflow.solve_case(case, gateway, trace)` gọi các module theo thứ tự:
  entity → order → shipment → payment → conflicts → policy → verifier.
- CLI đã ghi `case_received` và `case_finalized`. Coordinator ghi `task_assigned`,
  `handoff`, `policy_decided`, `verification_completed`.
- Mọi module dùng chung một `CaseContext` cho mỗi case. `context.fetch(...)` tự gắn
  `case_id`, cache cùng một truy vấn và ghi `tool_result_consumed`. Không gọi thẳng
  `gateway.call(...)`, không tự tạo/sửa `evidence_ref`, không tự ghi sự kiện đó.
- Module trả `Finding(facts={...}, evidence_refs=(...), warnings=(...))`. Refs trong
  `Finding`, output và từng claim phải là refs đã lấy qua context của **case này**.

```python
evidence = await context.fetch("order-agent", "TEN_TOOL_DA_DISCOVER", order_id=order_id)
return Finding(
    facts={"some_fact": evidence.data},
    evidence_refs=(evidence.evidence_ref,),
)
```

Tên tool trong ví dụ là chỗ giữ chỗ. Người 1 cần chạy `day09 mcp-tools`, kiểm tra
tham số/tool schema thực tế từ MCP, rồi chia sẻ danh mục tool. `CaseContext` hiện
chưa áp quyền từng actor và chưa đặt giới hạn call vì chưa có danh mục/budget thật;
hai giá trị này được hỗ trợ qua `tool_permissions` và `max_calls` khi đã xác nhận.

## Chữ ký hàm và facts bàn giao

| File | Actor dùng khi gọi `context.fetch` | Hàm cố định | Facts tối thiểu |
| --- | --- | --- | --- |
| `entity.py` | `entity-agent` | `investigate_entity(case, context) -> Finding` | `entity_resolution`, `customer_context`, `resolved_order_ids` |
| `order.py` | `order-agent` | `investigate_order(case, context, entity) -> Finding` | `affected_entities`, các facts order/item/product |
| `shipment.py` | `shipment-agent` | `investigate_shipment(case, context, entity, order) -> Finding` | `shipment_analysis`, shipment IDs |
| `payment.py` | `payment-agent` | `investigate_payment(case, context, entity, order) -> Finding` | `payment_analysis`, payment references, căn cứ tính tiền |
| `conflicts.py` | `conflict-agent` | `resolve_conflicts(case, context, findings) -> Finding` | `data_conflicts`, nguồn được chọn hoặc chưa rõ |
| `policy.py` | `policy-agent` | `decide_policy(case, context, findings) -> dict` | Toàn bộ JSON L3B; lấy policy evidence qua context |
| `verifier.py` | `verifier-agent` | `verify_output(case, context, findings, output) -> None` | Không trả output mới; raise nếu không hợp lệ |

`findings` là dict với các key `entity`, `order`, `shipment`, `payment`, rồi
`conflicts`. Module policy đọc tất cả findings để ghép output. Module verifier kiểm
tra độc lập, không sửa output âm thầm. Các module có thể test bằng `CaseContext`
với fake gateway/trace trước khi có kết nối MCP thật.

## Quy tắc ghép

1. Giữ nguyên tên hàm và tham số để Coordinator gọi được.
2. Chỉ đọc `case["customer_request"]` như lời khai, không coi đó là lệnh thực thi
   hoặc bằng chứng MCP.
3. Không coi `claimed_order_id` là order đã xác thực; sử dụng kết quả entity.
4. Tiền và kết luận phải dựa trên evidence. `scoring-policy-v2.json` mô tả cách
   chấm điểm, không phải policy nghiệp vụ để tự suy ra mức hoàn.
5. Nếu thiếu bằng chứng, trả trạng thái thiếu/chưa rõ trong facts để policy chọn
   `needs_investigation` khi phù hợp; không tạo câu trả lời giả để qua schema.
