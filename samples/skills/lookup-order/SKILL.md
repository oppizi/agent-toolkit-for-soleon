---
description: Look up an order by its id and summarize status, items, and delivery estimate.
argument-hint: "<order-id>"
---
Given an order id, retrieve the order record and report, in this order:

1. Current status (placed / in transit / delivered / exception).
2. Line items and quantities.
3. Delivery estimate or actual delivery date.

If the order id is not found, say so plainly and ask the customer to confirm
the id — never guess at a different order.
