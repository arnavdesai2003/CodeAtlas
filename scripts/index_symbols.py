from app.db.database import SessionLocal
from app.indexer.symbols import index_repository_symbols


def main():
    db = SessionLocal()

    try:
        result = index_repository_symbols(
            db=db,
            repository_id=1,
        )

        print("Symbol indexing completed")
        print()

        for key, value in result.items():
            print(f"{key}: {value}")

    finally:
        db.close()


if __name__ == "__main__":
    main()