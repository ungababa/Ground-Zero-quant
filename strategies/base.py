from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    @abstractmethod
    def run(self):
        pass