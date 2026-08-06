import RandomVariable

class ContiniousRandomVariable(RandomVariable):
    def _init_(self, name:str, lowerBound: float, upperBound: float):
        super().__init__(name, ((lowerBound + upperBound) / 2))
    def getLower(self):
        return self.lowerBound
    def getUpper(self):
        return self.upperBound
    
    
