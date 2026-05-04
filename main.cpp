#include <iostream>

double getNumber(){
	double num{};
	std::cout << "Please insert a number of your choice: ";
	std::cin >> num;
	return num;
}

char getSymbol(){
	char sym{};
	std::cout << "Enter +, -, *, or /: ";
	std::cin >> sym;
	return sym;
}

void Calculations(double first, double second, char symbol){
	if (symbol == '+')
		std::cout << first << symbol << second << " is " << first + second;
	else if (symbol == '-')
		std::cout << first << symbol << second << " is " << first - second;
	else if (symbol == '*')
		std::cout << first << symbol << second << " is " << first * second;
	else if (symbol == '/')
		std::cout << first << symbol << second << " is " << first / second;
	else
		std::cout << "error \n";
}	

int main(){
	double first{ getNumber() };
	double second{ getNumber() };
	char symbol{ getSymbol() };
	Calculations(first, second, symbol);
}