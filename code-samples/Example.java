public class Example {
    record User(String name, int score) {}

    public static void main(String[] args) {
        User[] users = {
            new User("Ada", 98),
            new User("Linus", 91),
            new User("Grace", 100)
        };

        int total = 0;
        for (User user : users) {
            total += user.score();
            System.out.printf("%s: %d%n", user.name(), user.score());
        }
        System.out.printf("Average: %.2f%n", total / (double) users.length);
    }
}
