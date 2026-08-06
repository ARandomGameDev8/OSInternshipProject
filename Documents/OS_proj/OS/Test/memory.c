#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main()
{
    size_t size = 1024 * 1024;

    while(1)
    {
        void *ptr = malloc(size);

        if(ptr)
        {
            printf("Allocated 1MB\n");

            // Touch memory so OS commits it
            for(size_t i=0;i<size;i++)
            {
                ((char*)ptr)[i] = 1;
            }
        }

        sleep(1);
    }

    return 0;
}