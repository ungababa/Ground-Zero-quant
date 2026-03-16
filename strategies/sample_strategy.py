from strategies.base import BaseStrategy
from src.roostoo.client import RoostooClient


class SampleStrategy(BaseStrategy):
    def __init__(self):
        self.client = RoostooClient()

    def run(self):
        ticker = self.client.get_ticker("BTC/USD")
        print("Ticker:", ticker)

        balance = self.client.get_balance()
        print("Balance:", balance)

        # Example placeholder logic
        # last_price = ticker["Data"]["BTC/USD"]["LastPrice"]
        # if last_price < 50000:
        #     result = self.client.place_order("BTC/USD", "BUY", 0.001)
        #     print(result)