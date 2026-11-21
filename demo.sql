-- minidb demo: run with  python3 cli.py demo.db --file demo.sql
CREATE TABLE users (id INT, name TEXT, age INT);
INSERT INTO users VALUES (1, 'Alice', 30);
INSERT INTO users VALUES (2, 'Bob', 25);
INSERT INTO users VALUES (3, 'Carol', 35);
INSERT INTO users VALUES (4, 'Dave', 28);
CREATE INDEX idx_users_age ON users (age);
SELECT * FROM users;
SELECT name, age FROM users WHERE age > 28;
SELECT * FROM users WHERE age = 30;
EXPLAIN SELECT * FROM users WHERE age = 30;
EXPLAIN SELECT * FROM users WHERE name = 'Bob';
SHOW TABLES;
DESCRIBE users;
