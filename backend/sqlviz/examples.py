DATASET = """
CREATE TABLE departments (id INT PRIMARY KEY, name VARCHAR(50));
INSERT INTO departments VALUES (1,'Engineering'),(2,'Design'),(3,'Support');
CREATE TABLE employees (
  id INT PRIMARY KEY, name VARCHAR(50), department_id INT,
  manager_id INT, salary DECIMAL(10,2)
);
INSERT INTO employees VALUES
  (1,'Ada',1,NULL,120000),(2,'Lin',1,1,95000),(3,'Sam',1,1,95000),
  (4,'Maya',2,NULL,105000),(5,'Noor',2,4,85000),(6,'Alex',NULL,NULL,70000);
CREATE TABLE sales (id INT PRIMARY KEY, employee_id INT, amount DECIMAL(10,2), sold_on DATE);
INSERT INTO sales VALUES
  (1,1,400,'2025-01-01'),(2,2,200,'2025-01-02'),
  (3,2,300,'2025-01-03'),(4,4,150,'2025-01-02'),(5,5,0,'2025-01-04');
"""

EXAMPLES = [
    dict(name="Joins, grouping and HAVING", sql="""SELECT d.name, COUNT(e.id) AS people, AVG(e.salary) AS avg_salary
FROM departments AS d
LEFT JOIN employees AS e ON e.department_id = d.id
GROUP BY d.id, d.name
HAVING COUNT(e.id) > 0
ORDER BY avg_salary DESC;"""),
    dict(name="Window ranks and running totals", sql="""SELECT name, department_id, salary,
  DENSE_RANK() OVER (PARTITION BY department_id ORDER BY salary DESC) AS salary_rank,
  SUM(salary) OVER (
    PARTITION BY department_id ORDER BY id
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ) AS running_salary
FROM employees
ORDER BY department_id, id;"""),
    dict(name="Chained CTEs", sql="""WITH totals AS (
  SELECT employee_id, SUM(amount) AS total FROM sales GROUP BY employee_id
), leaders AS (
  SELECT employee_id, total FROM totals WHERE total >= 300
)
SELECT e.name, l.total
FROM leaders AS l JOIN employees AS e ON e.id = l.employee_id
ORDER BY l.total DESC;"""),
    dict(name="Recursive CTE", sql="""WITH RECURSIVE numbers(n) AS (
  SELECT 1
  UNION ALL
  SELECT n + 1 FROM numbers WHERE n < 6
)
SELECT n, n * n AS squared FROM numbers ORDER BY n;"""),
    dict(name="Correlated EXISTS", sql="""SELECT e.name, e.salary
FROM employees AS e
WHERE EXISTS (
  SELECT 1 FROM sales AS s
  WHERE s.employee_id = e.id AND s.amount >= 300
)
ORDER BY e.name;"""),
    dict(name="UNION and duplicate removal", sql="""SELECT department_id AS department FROM employees WHERE salary >= 95000
UNION
SELECT id AS department FROM departments WHERE name = 'Support'
ORDER BY department;"""),
    dict(name="UPDATE preview", sql="""UPDATE employees
SET salary = salary + 1000
WHERE department_id = 1;"""),
]
