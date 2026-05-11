#include <iostream>
#include <string>
#include <string_view>

std::string getName(int num){
    std::string name{};
    std::cout << "Enter the name of person #" << num << ": ";
    std::getline(std::cin >> std::ws, name);
    return name;
}

int getAge(std::string_view Name){
    int age{};
    std::cout << "Enter the age of " << Name << ": ";
    std::cin >> age;
    return age;
}

void WhichOlder(std::string_view firstPerson, int firstAge, std::string_view secondPerson, int secondAge)
{
    if (firstAge < secondAge)
        std::cout << firstPerson << "(age " << firstAge << ")" << " is younger than " << secondPerson <<"(age " << secondAge << ")";
    else if (firstAge > secondAge)
    std::cout << firstPerson << "(age " << firstAge << ")" << " is older than " << secondPerson <<"(age " << secondAge << ")";
}
 
int main(){
    const std::string firstName{ getName(1) };
    const int firstAge{ getAge(firstName) };

    const std::string secondName{ getName(2) };
    const int secondAge{ getAge(secondName) };

    WhichOlder(firstName, firstAge, secondName, secondAge);

    return 0;
}