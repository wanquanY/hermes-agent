// JavaScript sample: async/await, objects, array methods.
const users = [
  { name: 'Ada', role: 'admin' },
  { name: 'Linus', role: 'developer' },
  { name: 'Grace', role: 'scientist' },
];

async function fetchGreeting(name) {
  return Promise.resolve(`Hello, ${name}!`);
}

async function main() {
  console.log('JavaScript sample');
  console.table(users);
  const greetings = await Promise.all(users.map((user) => fetchGreeting(user.name)));
  console.log(greetings.join('
'));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
