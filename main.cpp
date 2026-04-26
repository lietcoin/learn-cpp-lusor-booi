#include <iostream>

int main(){
    // Load integer numebrs from input
    int a{}, b{};

    std::cout << "Enter an integer: ";
    std::cin >> a;

    std::cout << "Enter another integer: ";
    std::cin >> b;

    int sumPlus{a + b};
    int sumMinus{a - b};

    std::cout << "The sum of " << a << " and " << b << " is: " << sumPlus << ".\n";
    std::cout << "The sum minus of " << a << " and " << b << " is: " << sumMinus << ".\n";

    return 0;
}