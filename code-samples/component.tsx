import React, { useMemo, useState } from 'react';

type Todo = {
  id: number;
  text: string;
  done: boolean;
};

export function TodoList() {
  const [todos, setTodos] = useState<Todo[]>([
    { id: 1, text: 'Create sample files', done: true },
    { id: 2, text: 'Test editor features', done: false },
  ]);

  const remaining = useMemo(() => todos.filter((todo) => !todo.done).length, [todos]);

  return (
    <section>
      <h2>Todo List</h2>
      <p>{remaining} item(s) remaining</p>
      <ul>
        {todos.map((todo) => (
          <li key={todo.id}>
            <label>
              <input
                type="checkbox"
                checked={todo.done}
                onChange={() =>
                  setTodos((items) =>
                    items.map((item) =>
                      item.id === todo.id ? { ...item, done: !item.done } : item,
                    ),
                  )
                }
              />
              {todo.text}
            </label>
          </li>
        ))}
      </ul>
    </section>
  );
}
