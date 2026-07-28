#[derive(Debug)]
struct User {
    name: String,
    score: u32,
}

fn average(scores: &[u32]) -> f64 {
    if scores.is_empty() {
        return 0.0;
    }
    scores.iter().sum::<u32>() as f64 / scores.len() as f64
}

fn main() {
    let users = vec![
        User { name: "Ada".into(), score: 98 },
        User { name: "Linus".into(), score: 91 },
        User { name: "Grace".into(), score: 100 },
    ];
    let scores: Vec<u32> = users.iter().map(|user| user.score).collect();
    println!("Rust sample: {:?}", users);
    println!("Average score: {:.2}", average(&scores));
}
