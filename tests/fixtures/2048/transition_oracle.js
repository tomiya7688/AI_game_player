// Test-only oracle: load the hash-verified upstream code, never a runtime input.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const sourceRoot = process.argv[2];
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
let draws = [];
const math = Object.create(Math);
math.random = () => {
  if (!draws.length) throw new Error("oracle exhausted its explicit random draws");
  return draws.shift();
};
const context = vm.createContext({Math: math});
for (const name of ["tile.js", "grid.js", "game_manager.js"]) {
  vm.runInContext(fs.readFileSync(path.join(sourceRoot, "js", name), "utf8"), context);
}
const results = cases.map(test => {
  draws = [...(test.draws || [])];
  const manager = Object.create(context.GameManager.prototype);
  manager.size = 4;
  manager.startTiles = 2;
  manager.actuate = () => {};
  manager.actuator = {continueGame() {}};
  manager.storageManager = {clearGameState() {}, getGameState() { return null; }};
  manager.grid = new context.Grid(4);
  manager.score = test.state.score || 0;
  manager.won = test.state.won || false;
  manager.over = test.state.over || false;
  manager.keepPlaying = test.state.keep_playing || false;
  test.state.cells.forEach((row, y) => row.forEach((value, x) => {
    if (value) manager.grid.insertTile(new context.Tile({x, y}, value));
  }));
  const beforeScore = manager.score;
  const beforeBoard = JSON.stringify(test.state.cells);
  if (test.operation === "restart") manager.restart();
  else if (test.operation === "continue") context.GameManager.prototype.keepPlaying.call(manager);
  else {
    if (test.operation === "slide") manager.addRandomTile = () => {};
    manager.move({UP: 0, RIGHT: 1, DOWN: 2, LEFT: 3}[test.direction]);
  }
  const cells = Array.from({length: 4}, (_, y) => Array.from({length: 4}, (_, x) => {
    const tile = manager.grid.cells[x][y];
    return tile ? tile.value : 0;
  }));
  return {cells, score: manager.score, won: manager.won, over: manager.over,
    keep_playing: manager.keepPlaying, score_delta: manager.score - beforeScore,
    moved: beforeBoard !== JSON.stringify(cells), unused_draws: draws.length};
});
process.stdout.write(JSON.stringify(results));
