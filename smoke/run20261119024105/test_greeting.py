from smoke.run20261119024105.greeting import greet


def test_greet_returns_name():
    assert greet("Ada") == "Hello, Ada!"
