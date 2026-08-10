#include <iostream.
#include <thread>
#include <vector>
#include <chrono>


void worker(int id)
{
    while(true)
    {
        volatile long x = 0;

        for(long i=0;i<10000000;i++)
            x += i;

        std::cout << "Thread "
                  << id
                  << " working\n";

        std::this_thread::sleep_for(
            std::chrono::milliseconds(500)
        );
    }
}


int main()
{
    std::vector<std::thread> threads;


    for(int i=0;i<5;i++)
    {
        threads.emplace_back(worker,i);
    }


    for(auto &t:threads)
        t.join();


    return 0;
}