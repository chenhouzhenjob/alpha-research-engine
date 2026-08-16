from lp_backtest.config import is_whitelisted_pool

CAKE = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
WBNB = "0xbb4CDB9CBd36B01bD1cBaEBF2de08d9173bc095c"
RANDOM_TOKEN = "0x1111111111111111111111111111111111111111"


def test_whitelisted_pool_matches_case_insensitively():
    assert is_whitelisted_pool(CAKE, RANDOM_TOKEN)
    assert is_whitelisted_pool(RANDOM_TOKEN, WBNB)


def test_non_whitelisted_pool_rejected():
    assert not is_whitelisted_pool(RANDOM_TOKEN, RANDOM_TOKEN)
