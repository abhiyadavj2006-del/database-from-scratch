-- minidb demo: run with  python3 cli.py demo.db --file demo.sql
CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT, email TEXT UNIQUE);
INSERT INTO users VALUES (1, 'Alice', 30, 'alice@example.com');
INSERT INTO users VALUES (2, 'Bob', 25, 'bob@example.com');
INSERT INTO users VALUES (3, 'Carol', 35, 'carol@example.com');
INSERT INTO users (id, name, age) VALUES (4, 'Dave', 28);
CREATE INDEX idx_users_age ON users (age);
SELECT * FROM users;
SELECT name, age FROM users WHERE age > 28;
SELECT * FROM users WHERE age = 30;
SELECT name FROM users ORDER BY age DESC LIMIT 2;
EXPLAIN SELECT * FROM users WHERE age = 30;
EXPLAIN SELECT * FROM users WHERE name = 'Bob';
BEGIN;
UPDATE users SET age = 31 WHERE id = 1;
ROLLBACK;
SELECT * FROM users WHERE id = 1;
SHOW TABLES;
DESCRIBE users;
