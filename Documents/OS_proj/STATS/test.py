from .RandomVariable import RandomVariable
from .ContiniousRandomVariable import ContiniousRandomVariable
from .DiscreteRandomVariable import DiscreteRandomVariable

from .Univariate.Distributions import ContiniousDistribution
from .Univariate.Distributions import DiscreteDistribution

from .Bivariate.Plots import ScatterPlot
from .Bivariate.Plots import DiscreteFrequencyTable


def testFrequencyTable():

    # X random variables
    X_variables = [
        DiscreteRandomVariable("X1", 1),
        DiscreteRandomVariable("X2", 2),
        DiscreteRandomVariable("X3", 3)
    ]


    # Y random variables
    Y_variables = [
        DiscreteRandomVariable("Y1", 10),
        DiscreteRandomVariable("Y2", 20),
        DiscreteRandomVariable("Y3", 30)
    ]


    # Frequencies for X and Y distributions
    freq = [1, 1, 1]


    X = DiscreteDistribution(
        "X Distribution",
        X_variables,
        freq
    )


    Y = DiscreteDistribution(
        "Y Distribution",
        Y_variables,
        freq
    )


    # Joint frequency table
    #
    #          Y1  Y2  Y3
    #
    # X1       2   3   1
    # X2       4   5   2
    # X3       1   2   6
    #

    frequency_table = [
        [2, 3, 1],
        [4, 5, 2],
        [1, 2, 6]
    ]


    table = DiscreteFrequencyTable(
        "X,Y Frequency Table",
        X,
        Y,
        frequency_table
    )


    print("Probability Table:")
    
    probability = table.ConvertFrequencyToProbability()

    for row in probability:
        print(row)


    print("\nFrequency Cell X2,Y3:")
    print(
        table.getFrequencyCell(
            X_variables[1],
            Y_variables[2]
        )
    )


    print("\nProbability Cell X2,Y3:")
    print(
        table.getProbabilityCell(
            X_variables[1],
            Y_variables[2]
        )
    )


    print("\nRow of X1:")
    print(
        table.getRow(0)
    )


    print("\nColumn of Y2:")
    print(
        table.getColumn(1)
    )


    print("\nP(Y | X1):")
    print(
        table.getConditional_Y_Knowing_X(
            X_variables[0]
        )
    )


    print("\nP(X | Y2):")
    print(
        table.getConditional_X_Knowing_Y(
            Y_variables[1]
        )
    )



def main():

    random_discrete_variables = [
        DiscreteRandomVariable("v" + str(i), i)
        for i in range(9)
    ]

    freq_list = [
        i ** 2
        for i in range(9)
    ]

    dist1 = DiscreteDistribution(
        "Dist1",
        random_variables=random_discrete_variables,
        frequency=freq_list
    )

    dist1.printDistribution()
    dist1.GraphDistribution()
    ContiniousRV = [ContiniousRandomVariable("", 0,5), ContiniousRandomVariable("", 5,10), ContiniousRandomVariable("",10,13), ContiniousRandomVariable("", 13, 20), ContiniousRandomVariable("", 20, 25)]
    Dist2 = ContiniousDistribution("Dist2", random_variables=ContiniousRV, frequency=[i ** 2 for i  in range(0 , len(ContiniousRV))])
    Dist2.GraphDistribution()
    
    X_variables = [
        DiscreteRandomVariable("x1", 1),
        DiscreteRandomVariable("x2", 2),
        DiscreteRandomVariable("x3", 3),
        DiscreteRandomVariable("x4", 4),
        DiscreteRandomVariable("x5", 5)
    ]
    Y_variables = [
        DiscreteRandomVariable("y1", 2),
        DiscreteRandomVariable("y2", 4),
        DiscreteRandomVariable("y3", 5),
        DiscreteRandomVariable("y4", 8),
        DiscreteRandomVariable("y5", 10)
    ]
    freq = [
        1,
        1,
        1,
        1,
        1
    ]
    X = DiscreteDistribution(
        "X Distribution",
        X_variables,
        freq
    )


    Y = DiscreteDistribution(
        "Y Distribution",
        Y_variables,
        freq
    )
    
    
    
    
    scatter = ScatterPlot(
        "Simple Linear Relation",
        X,
        Y
    )
    scatter.printStatistics()
    scatter.plot()
    testFrequencyTable()
    
    


if __name__ == "__main__":
    main()