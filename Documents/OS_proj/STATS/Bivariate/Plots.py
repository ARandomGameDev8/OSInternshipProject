from Documents.OS_proj.STATS.Univariate.Distributions import ContiniousDistribution
from Documents.OS_proj.STATS.Univariate.Distributions import DiscreteDistribution
from Documents.OS_proj.STATS.RandomVariable import RandomVariable
import matplotlib.pyplot as plt
import numpy as np


class ScatterPlot:

    def __init__(self, name: str, X, Y):
        if not isinstance(X, (DiscreteDistribution, ContiniousDistribution)):
            raise TypeError("X must be a DiscreteDistribution or ContinuousDistribution")
        if not isinstance(Y, (DiscreteDistribution, ContiniousDistribution)):
            raise TypeError("Y must be a DiscreteDistribution or ContinuousDistribution")
        if len(X.random_variables) != len(Y.random_variables):
            raise ValueError("X and Y must contain the same number of observations.")
 
        self.name = name
        self.X    = X
        self.Y    = Y
    
    def getMeanX(self):
        return self.X.getMean()

    def getMeanY(self):
        return self.Y.getMean()

    def getVarianceX(self):
        return self.X.getVariance()

    def getVarianceY(self):
        return self.Y.getVariance()

    def getStandardDeviationX(self):
        return self.X.getStandardDeviation()

    def getStandardDeviationY(self):
        return self.Y.getStandardDeviation()

    # -------------------------
    # Covariance
    # -------------------------

    def getCovariance(self):

        meanX = self.getMeanX()
        meanY = self.getMeanY()

        total = 0

        for x, y in zip(self.X.random_variables,
                        self.Y.random_variables):

            total += (x.getVal() - meanX) * (y.getVal() - meanY)

        return total / len(self.X.random_variables)

    # -------------------------
    # Correlation
    # -------------------------

    def getCorrelationCoefficient(self):

        return self.getCovariance() / (
            self.getStandardDeviationX() *
            self.getStandardDeviationY()
        )

    # -------------------------
    # Regression Y on X
    # -------------------------

    def getRegressionSlope(self):

        return self.getCovariance() / self.getVarianceX()

    def getRegressionIntercept(self):

        return self.getMeanY() - self.getRegressionSlope() * self.getMeanX()

    def regressionEquation(self):

        a = self.getRegressionIntercept()
        b = self.getRegressionSlope()

        return f"y = {a:.4f} + {b:.4f}x"

    # -------------------------
    # Plot
    # -------------------------

    def plot(self):

        x = [rv.getVal() for rv in self.X.random_variables]
        y = [rv.getVal() for rv in self.Y.random_variables]

        plt.figure(figsize=(8,6))

        plt.scatter(x, y,
                    color="blue",
                    label="Observations")

        # Regression line

        b = self.getRegressionSlope()
        a = self.getRegressionIntercept()

        xLine = np.linspace(min(x), max(x), 100)
        yLine = a + b * xLine

        plt.plot(
            xLine,
            yLine,
            color="red",
            linewidth=2,
            label="Regression Line"
        )

        plt.title(self.name)

        plt.xlabel(self.X.name)
        plt.ylabel(self.Y.name)

        plt.grid(True)

        plt.legend()

        plt.show()

    # -------------------------
    # Print Statistics
    # -------------------------

    def printStatistics(self):

        print("Scatter Plot:", self.name)
        print("--------------------------------")
        print("Mean X:", self.getMeanX())
        print("Mean Y:", self.getMeanY())
        print("Variance X:", self.getVarianceX())
        print("Variance Y:", self.getVarianceY())
        print("Std Dev X:", self.getStandardDeviationX())
        print("Std Dev Y:", self.getStandardDeviationY())
        print("Covariance:", self.getCovariance())
        print("Correlation (r):", self.getCorrelationCoefficient())
        print("Regression:", self.regressionEquation())
        
        
class DiscreteFrequencyTable:
        def __init__(self, name :str, X, Y, frequency: list = None):
            if not isinstance(X, (DiscreteDistribution, ContiniousDistribution)):
                raise TypeError("X must be a DiscreteDistribution ot ContiniousDistribution")
            if not isinstance(Y, (DiscreteDistribution, ContiniousDistribution)):
                raise TypeError("X must be a DiscreteDistribution ot ContiniousDistribution")
            if len(X.random_variables) != len(Y.random_variables):
                raise ValueError("X and Y must contain the same number of observations.")
            self.name = name
            self.X = X
            self.Y = Y
            self.frequency = frequency if frequency is not None else []
                
            
            
        
        
        def getMeanX(self):
            return self.X.getMean()
        def getMeanY(self): 
            return self.Y.getMean()
        def getVarianceX(self):        
            return self.X.getVariance() 
        def getVarianceY(self):
            return self.Y.getVariance() 
        def getStandardDeviationX(self):
            return self.X.getStandardDeviation()    
        def getStandardDeviationY(self):                
            return self.Y.getStandardDeviation()        
        def getCovariance(self):
            meanX = self.getMeanX()
            meanY = self.getMeanY()
            total = 0
            for x, y in zip(self.X.random_variables, self.Y.random_variables):
                total += (x.getVal() - meanX) * (y.getVal() - meanY)
            return total / len(self.X.random_variables)
        def setFrequency(self, frequency:list):
            self.frequency = frequency
        def setFrequencyCell(self, indexI:int, indexJ:int, value:float):
            self.frequency[indexI][indexJ] = value
        def setFrequencyCell(self, x:RandomVariable, y:RandomVariable, value:float):
            indexI = self.X.random_variables.index(x)
            indexJ = self.Y.random_variables.index(y)
            self.frequency[indexI][indexJ] = value
        def getFrequencyCell(self, indexI:int, indexJ:int):
            return self.frequency[indexI][indexJ]
        def getFrequencyCell(self, x:RandomVariable, y:RandomVariable):
            indexI = self.X.random_variables.index(x)
            indexJ = self.Y.random_variables.index(y)
            return self.frequency[indexI][indexJ]   
        def getRow(self, indexI:int):
            return self.frequency[indexI]
        def getColumn(self, indexJ:int):
            return [row[indexJ] for row in self.frequency]
        def ConvertFrequencyToProbability(self):
            total = sum(sum(row) for row in self.frequency)
            return [[cell / total for cell in row] for row in self.frequency]
        def getProbabilityCell(self, indexI:int, indexJ:int):
            probabilityTable = self.ConvertFrequencyToProbability()
            return probabilityTable[indexI][indexJ]
        def getProbabilityCell(self, x:RandomVariable, y:RandomVariable):
            indexI = self.X.random_variables.index(x)
            indexJ = self.Y.random_variables.index(y)
            probabilityTable = self.ConvertFrequencyToProbability()
            return probabilityTable[indexI][indexJ] 
        def getConditional_Y_Knowing_X(self, x:RandomVariable):
            indexI = self.X.random_variables.index(x)
            row = self.getRow(indexI)
            total = sum(row)
            return [cell / total for cell in row]
        def getConditional_X_Knowing_Y(self, y:RandomVariable):
            indexJ = self.Y.random_variables.index(y)
            column = self.getColumn(indexJ)
            total = sum(column)
            return [cell / total for cell in column]
        
        