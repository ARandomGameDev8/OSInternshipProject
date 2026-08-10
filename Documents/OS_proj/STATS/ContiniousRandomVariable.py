from .RandomVariable import RandomVariable


class ContiniousRandomVariable(RandomVariable):

    def __init__(self, name: str, lowerBound: float, upperBound: float):

        self.lowerBound = lowerBound
        self.upperBound = upperBound

        # Store midpoint as the value of the random variable
        super().__init__(
            name,
            (lowerBound + upperBound) / 2
        )


    def getLower(self):
        return self.lowerBound


    def getUpper(self):
        return self.upperBound