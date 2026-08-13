
from Documents.OS_proj.STATS.ContiniousRandomVariable import ContiniousRandomVariable
from Documents.OS_proj.STATS.DiscreteRandomVariable import DiscreteRandomVariable
from Documents.OS_proj.STATS.RandomVariable import RandomVariable
import matplotlib.pyplot as plt

class ContiniousDistribution:
    def __init__(self, name:str, random_variables: list[ContiniousRandomVariable], frequency:  list[float]):
        self.name = name
        self.random_variables = random_variables
        self.frequency = frequency
    def getMean(self):
        total = 0
        for i in range(len(self.random_variables)):
            total += self.random_variables[i].getVal() * self.frequency[i]
        return total / sum(self.frequency)
    def getVariance(self):
        mean = self.getMean()
        total = 0
        for i in range(len(self.random_variables)):
            total += ((self.random_variables[i].getVal() - mean) ** 2) * self.frequency[i]
        return total / sum(self.frequency)  
    def getStandardDeviation(self):
        return self.getVariance() ** 0.5        
    def getCV(self):    
        return self.getrStandardDeviation() / self.getMean()    
    def printDistribution(self):        
        print(f"Distribution Name: {self.name}")
        print("Random Variables:")
        for i in range(len(self.random_variables)):
            print(f"Name: {self.random_variables[i].getName()}, Value: {self.random_variables[i].getVal()}, Frequency: {self.frequency[i]}")
            
    def ConvertFrequencyToProbability(self):
        total_frequency = sum(self.frequency)
        probability = [f / total_frequency for f in self.frequency]    
        return probability
    def ConvertProbabilitytoDensity(self):
        Probability = self.ConvertFrequencyToProbability()
        total_probability = sum(Probability)
        density = [p / total_probability for p in Probability]
        return density
    def getMode(self):
        max_frequency = max(self.frequency)
        mode_index = self.frequency.index(max_frequency)
        return self.random_variables[mode_index].getVal()
    def getMedian(self):
        sorted_variables = sorted(zip(self.random_variables, self.frequency), key=lambda x: x[0].getVal())
        total_frequency = sum(self.frequency)
        cumulative_frequency = 0
        for rv, freq in sorted_variables:
            cumulative_frequency += freq
            if cumulative_frequency >= total_frequency / 2:
                return rv.getVal()
        
            
    def GraphDistribution(self):

        widths = [
            rv.upperBound - rv.lowerBound
            for rv in self.random_variables
        ]

        probabilities = self.ConvertFrequencyToProbability()

        densities = [
            p / w
            for p, w in zip(probabilities, widths)
        ]

        left_edges = [
            rv.lowerBound
            for rv in self.random_variables
        ]

        plt.bar(
            left_edges,
            densities,
            width=widths,
            align="edge",
            edgecolor="black"
        )

        plt.xlabel("X")
        plt.ylabel("Density")
        plt.title(f"Continuous Distribution: {self.name}")

        plt.show()

            
class DiscreteDistribution:
    def __init__(self, name:str, random_variables: list[DiscreteRandomVariable], frequency:  list[float]):
        self.name = name
        self.random_variables = random_variables
        self.frequency = frequency
    def getMean(self):
        total = 0
        for i in range(len(self.random_variables)):
            total += self.random_variables[i].getVal() * self.frequency[i]
        return total / sum(self.frequency)
    def getVariance(self):
        mean = self.getMean()
        total = 0
        for i in range(len(self.random_variables)):
            total += ((self.random_variables[i].getVal() - mean) ** 2) * self.frequency[i]
        return total / sum(self.frequency)
    def getStandardDeviation(self):
        return self.getVariance() ** 0.5    
    def getCV(self):    
        return self.getrStandardDeviation() / self.getMean()    
    def getMode(self):
        max_frequency = max(self.frequency)
        mode_index = self.frequency.index(max_frequency)
        return self.random_variables[mode_index].getVal()
    def getMedian(self):
        sorted_variables = sorted(zip(self.random_variables, self.frequency), key=lambda x: x[0].getVal())
        total_frequency = sum(self.frequency)
        cumulative_frequency = 0
        for rv, freq in sorted_variables:
            cumulative_frequency += freq
            if cumulative_frequency >= total_frequency / 2:
                return rv.getVal()
    def printDistribution(self):    
        print(f"Distribution Name: {self.name}")
        print("Random Variables:")
        for i in range(len(self.random_variables)):
            print(f"Name: {self.random_variables[i].getName()}, Value: {self.random_variables[i].getVal()}, Frequency: {self.frequency[i]}")    
    
   
   
    def GraphDistribution(self):
    

    
        x = [rv.getVal() for rv in self.random_variables]


        total_frequency = sum(self.frequency)
        y = [
            freq / total_frequency
            for freq in self.frequency
        ]

 
        plt.figure(figsize=(8, 5))

        plt.stem(
              x,
              y
         )

        plt.xlabel("Random Variable Value (X)")
        plt.ylabel("Probability P(X=x)")
        plt.title(f"Probability Distribution: {self.name}")

        plt.grid(True)
        plt.show()