# DECISIONS.md

**1. Indexes:** I used indexes to support employee-code lookups, attendance queries, and department/date-based analytics. Unique indexes help prevent duplicate employee codes and duplicate attendance records for the same employee and date. I avoided adding unnecessary indexes because every extra index increases storage and write costs.

**2. Punch-in race:** If two identical punch-in requests arrive together, both may initially check for an existing record. The database's unique constraint should allow only one matching attendance record. The other request must handle the duplicate-key error without creating a second record.

**3. Ties:** When employees have equal late minutes at the leaderboard cutoff, a deterministic secondary sort, such as employee code, makes the result repeatable. If the code sorts only by late minutes, tied employees may appear in an inconsistent order.

**4. Headcount:** Department headcount should be calculated from employee records, not attendance logs. This ensures employees with zero attendance logs are still counted.

**5. Scaling:** For 100 times more data, I would review query execution plans and index usage, reduce unnecessary aggregation work, and consider caching frequently requested analytics.
