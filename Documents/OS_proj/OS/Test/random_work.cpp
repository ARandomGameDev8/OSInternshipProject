#include <iostream>
#include <vector>
#include <cstdlib>
#include <ctime>
#include <unistd.h>


int main()
{
    srand(time(NULL));

    std::vector<char*> memory;


    while(true)
    {
        int action = rand()%3;


        if(action == 0)
        {
            char* block = new char[10*1024*1024];

            for(int i=0;i<10*1024*1024;i++)
                block[i]=1;

            memory.push_back(block);

            std::cout<<"Allocated 10MB\n";
        }


        else if(action == 1 && !memory.empty())
        {
            delete[] memory.back();
            memory.pop_back();

            std::cout<<"Freed memory\n";
        }


        else
        {
            std::cout<<"Idle\n";
        }


        sleep(2);
    }
}