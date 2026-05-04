// when ur back do the calculator again, remember void no return/ double, int and etc NEEDS and return variable
// also remember to use an function for everything separatle

#include <iostream>

double getNumber(){
    double number{};
    std::cout << "Please insert an number: ";
    std::cin >> number;
    return number;
}

char getSymbol(){
    char symbol;
    std::cout << "Please insert an mathematical symbol: ";
    std::cin >> symbol;
    return symbol;
}

double Calculations(double first, double second, char symbol){
    if (symbol == '+')
        return first + second;
    else if (symbol == '-')
        return first - second;
    else if (symbol == '*')
        return first * second;
    else if (symbol == '/')
        return first / second;
    else
        std::cout << "\"Something went wrong, expect to get answer 0.\" \n";
        return 0;
}

void PrintResults(double first, double second, char symbol){
    std::cout << first << symbol << second << " is equal to " << Calculations(first, second, symbol) << '\n';
}



int main(){
    double first{ getNumber() };
    double second{ getNumber() };
    char symbol{ getSymbol() };
    
    PrintResults(first, second, symbol);
}