class Base7:
    def __init__(self):
        pass

    def add(self, a, b):
        return (a + b) % 7

    def subtract(self, a, b):
        return (a - b) % 7

    def divide(self, a, b):
        return (a // b) % 7

    def multiply(self, a, b):
        return (a * b) % 7

    def test(self):
        assert self.add(6, 3) == 2, f"expected 2, got: {self.add(6, 3)}"
        assert self.subtract(6, 3) == 3, f"expected 3, got: {self.subtract(6, 3)}"
        assert self.divide(6, 3) == 2, f"expected 2, got: {self.divide(6, 3)}"
        assert self.multiply(6, 3) == 4, f"expected 4, got: {self.multiply(6, 3)}"
