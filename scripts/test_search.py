from app.search.engine import search_code


def main():
    query = "relu activation"

    results = search_code(
        query=query,
        limit=5,
    )

    print(f'Query: "{query}"')
    print(f"Results: {len(results)}")
    print()

    for rank, result in enumerate(results, start=1):
        print("=" * 70)
        print(f"Rank: {rank}")
        print(f"Score: {result['score']}")
        print(f"Symbol: {result['qualified_name']}")
        print(f"Type: {result['kind']}")
        print(f"File: {result['path']}")
        print(
            f"Lines: "
            f"{result['start_line']}-"
            f"{result['end_line']}"
        )


if __name__ == "__main__":
    main()