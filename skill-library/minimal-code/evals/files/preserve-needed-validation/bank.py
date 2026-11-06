def transfer(accounts: dict[str, int], source: str, target: str, amount: int) -> None:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if source == target:
        raise ValueError("source and target must differ")
    if source not in accounts or target not in accounts:
        raise KeyError("unknown account")
    balance = accounts[source]
    if balance < amount:
        raise ValueError("insufficient funds")
    new_balance = balance - amount
    accounts[source] = new_balance
    accounts[target] = accounts[target] + amount
