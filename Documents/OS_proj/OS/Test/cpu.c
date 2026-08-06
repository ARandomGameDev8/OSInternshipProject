#include <stdio.h>
#include <math.h>

int main()
{
    double x = 0;

    while(1)
    {
        for(int i = 0; i < 10000000; i++)
        {
            x += sin(i) * cos(i);
        }
    }

    return 0;
}