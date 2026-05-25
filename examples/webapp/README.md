# Webapp Demo (Form D)

This directory contains instructions for running the Spring Boot webapp demo.

## Prerequisites

- JDK 21 (temurin recommended)
- Maven 3.9+
- Python core with populated database (run `import_statements` first)

## Build and run

```bash
cd webapp
./mvnw package -DskipITs
java -jar target/ledger-agent-webapp-*.jar
# Visit http://localhost:8080
```

## Demo workflow

1. Open `http://localhost:8080`
2. Enter fiscal year (e.g. `2024`) and click **Run**
3. The results page shows summary cards:
   - Profit/Loss signal
   - Balance Sheet Health
   - PTE Due Signal
   - Tax Due Signal
   - Confidence Flags
4. Click **Show raw JSON** to expand the full API response

## Privacy

The webapp shares the same privacy firewall as the Python core. No real entity
names, partner names, or financial figures should appear in the UI beyond what
is already in the local database.

## Running smoke tests

```bash
cd webapp
./mvnw test -Dtest="*IT" -pl .
```

Note: Integration tests require a running instance. Start the server first.
