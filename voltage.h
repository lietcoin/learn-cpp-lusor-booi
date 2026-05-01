#include <iostream>

float getData(){
    float current{};
    float resistance{};
    std::cout << "Please, insert your current: ";
    std::cin >> current;

    std::cout << "Please, insert your resistance: ";
    std::cin >> resistance;

    return current * resistance;
}

void PrintResult(int voltage){
    std::cout << voltage << "v (volts).\n";
}