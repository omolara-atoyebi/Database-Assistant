"""Question -> SQL examples shown to the LLM. Each one covers a trap in this dataset.

tests/test_pipeline_context.py checks that every example's SQL passes the
guardrails and returns rows.
"""

FEW_SHOT_EXAMPLES = [
    {
        "lesson": "Dates are TEXT (YYYY-MM-DD); bucket them with strftime.",
        "question": "How many orders were placed each month?",
        "sql": (
            "SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS order_count\n"
            "FROM orders\n"
            "GROUP BY month\n"
            "ORDER BY month;"
        ),
    },
    {
        "lesson": (
            "Joining orders to order_items repeats total_amount once per item, so "
            "SUM(total_amount) over that join double-counts. Filter orders with IN instead."
        ),
        "question": "What is the total revenue from orders that include an Electronics product?",
        "sql": (
            "SELECT ROUND(SUM(total_amount), 2) AS revenue\n"
            "FROM orders\n"
            "WHERE id IN (\n"
            "    SELECT oi.order_id\n"
            "    FROM order_items oi\n"
            "    JOIN products p ON p.id = oi.product_id\n"
            "    WHERE p.category = 'Electronics'\n"
            ");"
        ),
    },
    {
        "lesson": "Revenue is quantity * order_items.unit_price (price at sale), not products.price.",
        "question": "What are the top 3 products by revenue?",
        "sql": (
            "SELECT p.name, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue\n"
            "FROM order_items oi\n"
            "JOIN products p ON p.id = oi.product_id\n"
            "GROUP BY p.id, p.name\n"
            "ORDER BY revenue DESC\n"
            "LIMIT 3;"
        ),
    },
    {
        "lesson": "The United Kingdom is stored as 'UK', not 'GB'. Use the known country codes.",
        "question": "How many customers are from Great Britain?",
        "sql": "SELECT COUNT(*) AS customer_count\nFROM customers\nWHERE country = 'UK';",
    },
    {
        "lesson": "Revenue by category per country joins all four tables, summing at item level.",
        "question": "What is the revenue for each product category in each country?",
        "sql": (
            "SELECT c.country, p.category,\n"
            "       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue\n"
            "FROM order_items oi\n"
            "JOIN orders o ON o.id = oi.order_id\n"
            "JOIN customers c ON c.id = o.customer_id\n"
            "JOIN products p ON p.id = oi.product_id\n"
            "GROUP BY c.country, p.category\n"
            "ORDER BY c.country, revenue DESC;"
        ),
    },
]
